"""Клиентские сценарии записи/отмены/переноса целиком: настоящий диспетчер и БД, Telegram — запись запросов.

Проверяем то, что не видно на уровне сервисов: тексты ответов, поведение при сбоях доставки и устаревших кнопках.
"""

import datetime as dt
import re

import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import content as client_content
from app.bot.handlers.client import my_booking
from app.bot.keyboards.callback_data import BookingActionCallback, ConfirmCallback, TimeCallback
from app.bot.middlewares.error_handling import GENERIC_ERROR_TEXT
from app.bot.states.booking_states import BookingFSM, RescheduleFSM
from app.database.models import Booking, BookingStatus, Service, WorkingHours
from app.services.booking_service import BookingService
from tests.conftest import MASTER_ID, RIGA_TZ, next_weekday, set_owner
from tests.tg import tg_harness

ADMIN_ID = 999
CLIENT_TG = 111111  # test_user
HOSTILE_SERVICE = "Брови & <b>ресницы</b>"


@pytest_asyncio.fixture
async def world(session, master_profile, test_user):
    """Рабочий день через 2+ недели, услуга с «враждебным» названием и запись клиента на 15:00."""
    await set_owner(session, ADMIN_ID)
    day = next_weekday(4)
    session.add(WorkingHours(master_id=MASTER_ID, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    service = Service(master_id=MASTER_ID, name=HOSTILE_SERVICE, price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)
    await session.commit()

    def at(hour: int) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(hour, 0), tzinfo=RIGA_TZ)

    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(test_user.id, service.id, at(15))
    await session.commit()
    return {"at": at, "service_id": service.id, "booking_id": booking.id}


async def _bookings(session) -> list[Booking]:
    session.expire_all()
    return list((await session.execute(select(Booking).order_by(Booking.id))).scalars())


async def _deactivate(session, service_id: int) -> None:
    session.expire_all()
    (await session.get(Service, service_id)).active = False
    await session.commit()


def _no_unescaped_html(text: str) -> None:
    assert "<" not in text and ">" not in text, text
    assert not re.search(r"&(?!(?:amp|lt|gt|quot|#x?\d+);)", text), text


# --- запись ---


async def _confirm(h, world, user_id=CLIENT_TG, hour=17):
    await h.set_state(user_id, BookingFSM.confirm, service_id=world["service_id"], selected_time=world["at"](hour).isoformat())
    await h.press(user_id, ConfirmCallback(action="yes"))


async def test_booking_confirmation_escapes_service_name_for_client_and_master(session, world):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _confirm(h, world)

        assert any("Запись подтверждена" in t for t in h.tg.edited())
        for text in h.tg.texts():
            _no_unescaped_html(text)
        assert any("Брови &amp; &lt;b&gt;ресницы&lt;/b&gt;" in t for t in h.tg.sent_to(ADMIN_ID))
    assert len(await _bookings(session)) == 2


async def test_booking_is_confirmed_even_if_master_blocked_the_bot(session, world):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        h.tg.blocked_chat_ids.add(ADMIN_ID)
        await _confirm(h, world)

        assert any("Запись подтверждена" in t for t in h.tg.edited())
        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown(), "клиенту не нужно видеть ошибку из-за недоставленного уведомления мастеру"
    assert len(await _bookings(session)) == 2


async def test_booking_confirmation_when_service_was_deactivated_meanwhile(session, world):
    await _deactivate(session, world["service_id"])
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _confirm(h, world)

        assert any("больше недоступна" in t for t in h.tg.edited())
        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown()
        assert await h.get_state(CLIENT_TG) is None
    assert len(await _bookings(session)) == 1


async def test_booking_confirmation_when_slot_was_just_taken(session, world):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _confirm(h, world, hour=15)  # 15:00 уже занято записью из фикстуры

        assert any("только что заняли" in t for t in h.tg.edited())
    assert len(await _bookings(session)) == 1


# --- отмена ---


async def test_cancellation_is_saved_even_if_master_blocked_the_bot(session, world):
    booking_id = world["booking_id"]
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        h.tg.blocked_chat_ids.add(ADMIN_ID)
        await h.press(CLIENT_TG, BookingActionCallback(booking_id=booking_id, action="cancel_confirm"))

        assert any("Запись отменена" in t for t in h.tg.edited())
        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown()
    assert (await _bookings(session))[0].status == BookingStatus.CANCELLED.value


async def test_cancelling_twice_gives_alert_not_error(session, world):
    booking_id = world["booking_id"]
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        action = BookingActionCallback(booking_id=booking_id, action="cancel_confirm")
        await h.press(CLIENT_TG, action)
        await h.press(CLIENT_TG, action)  # двойное нажатие / устаревшая кнопка

        assert h.tg.alerts() == ["Эту запись уже нельзя отменить."]
        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown()


# --- перенос ---


async def _pick_new_time(h, world, hour=17, user_id=CLIENT_TG):
    await h.set_state(user_id, RescheduleFSM.select_time, booking_id=world["booking_id"], service_duration=60)
    await h.press(user_id, TimeCallback(ts=int(world["at"](hour).timestamp())))


async def test_reschedule_moves_booking_and_escapes_service_name(session, world):
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        await _pick_new_time(h, world)

        assert any("Запись перенесена" in t for t in h.tg.edited())
        for text in h.tg.texts():
            _no_unescaped_html(text)
    old, new = await _bookings(session)
    assert (old.status, new.status) == (BookingStatus.CANCELLED.value, BookingStatus.CONFIRMED.value)


async def test_reschedule_survives_blocked_master(session, world):
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        h.tg.blocked_chat_ids.add(ADMIN_ID)
        await _pick_new_time(h, world)

        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown()
    assert [b.status for b in await _bookings(session)] == ["cancelled", "confirmed"]


async def test_reschedule_when_service_was_deactivated_keeps_old_booking(session, world):
    await _deactivate(session, world["service_id"])
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        await _pick_new_time(h, world)

        assert any("старая запись осталась в силе" in t for t in h.tg.edited())
        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown()
        assert await h.get_state(CLIENT_TG) is None
    assert [b.status for b in await _bookings(session)] == ["confirmed"]


async def test_reschedule_of_already_cancelled_booking_is_refused_politely(session, world):
    booking_id = world["booking_id"]
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        await h.press(CLIENT_TG, BookingActionCallback(booking_id=booking_id, action="cancel_confirm"))
        await _pick_new_time(h, world)  # кнопка «Перенести» на устаревшей карточке

        assert any("уже нельзя перенести" in t for t in h.tg.edited())
        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown()
    assert [b.status for b in await _bookings(session)] == ["cancelled"]


async def test_start_reschedule_of_cancelled_booking_shows_alert(session, world):
    booking_id = world["booking_id"]
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        await h.press(CLIENT_TG, BookingActionCallback(booking_id=booking_id, action="cancel_confirm"))
        await h.press(CLIENT_TG, BookingActionCallback(booking_id=booking_id, action="reschedule"))

        assert h.tg.alerts() == ["Эту запись уже нельзя перенести."]
        assert await h.get_state(CLIENT_TG) is None


# --- прайс ---


async def test_price_list_escapes_service_name_and_description(session, world):
    session.expire_all()
    (await session.get(Service, world["service_id"])).description = "Лёгкая & быстрая"
    await session.commit()
    async with tg_harness(client_content.router) as h:
        await h.send_text(CLIENT_TG, "💰 Услуги и цены")

        [text] = h.tg.sent_to(CLIENT_TG)
        _no_unescaped_html(text)
        assert "Брови &amp; &lt;b&gt;ресницы&lt;/b&gt;" in text and "Лёгкая &amp; быстрая" in text
