"""Лимит активных записей, серия из трёх отмен клиентом и решение мастера (ограничить / подтвердить доступ)."""

import datetime as dt

import pytest
import pytest_asyncio

from app.bot.handlers.admin import client_access
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import my_booking
from app.bot.keyboards.callback_data import AdminClientAccessCallback, BookingActionCallback
from app.services.access_request_service import OPENED_TEXT
from app.database.models import Booking, BookingLimitRequest, BookingStatus, LimitRequestStatus, Service, User, WorkingHours
from app.services.booking_service import BookingService
from app.services.exceptions import BookingLimitReachedError, ClientRestrictedError
from tests.conftest import MASTER_ID, RIGA_TZ, client_row, next_weekday, set_client, set_owner
from tests.tg import tg_harness

ADMIN_ID = 999
CLIENT_TG = 111111  # test_user
DAY = next_weekday(4)


def at(hhmm: str) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time.fromisoformat(hhmm), tzinfo=RIGA_TZ)


@pytest_asyncio.fixture(autouse=True)
async def _admin(session):
    await set_owner(session, ADMIN_ID)


async def _hours(session):
    session.add(WorkingHours(master_id=MASTER_ID, weekday=DAY.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    await session.commit()


async def _book(session, user, service, hhmm: str, **kwargs) -> Booking:
    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(user.id, service.id, at(hhmm), **kwargs)
    await session.commit()
    return booking


async def test_fourth_active_booking_is_rejected(session, test_user, manicure_service):
    await _hours(session)
    for hhmm in ("10:00", "12:00", "14:00"):
        await _book(session, test_user, manicure_service, hhmm, enforce_client_limits=True)
    with pytest.raises(BookingLimitReachedError):
        await _book(session, test_user, manicure_service, "16:00", enforce_client_limits=True)


async def test_completed_and_cancelled_bookings_free_a_place(session, test_user, manicure_service):
    await _hours(session)
    first = await _book(session, test_user, manicure_service, "10:00", enforce_client_limits=True)
    second = await _book(session, test_user, manicure_service, "12:00", enforce_client_limits=True)
    await _book(session, test_user, manicure_service, "14:00", enforce_client_limits=True)

    first.status = BookingStatus.COMPLETED.value
    await session.commit()
    await _book(session, test_user, manicure_service, "16:00", enforce_client_limits=True)  # снова три действующих

    with pytest.raises(BookingLimitReachedError):
        await _book(session, test_user, manicure_service, "17:00", enforce_client_limits=True)

    await BookingService(session, RIGA_TZ, master_id=MASTER_ID).cancel_booking(second.id)
    await session.commit()
    await _book(session, test_user, manicure_service, "17:00", enforce_client_limits=True)


async def test_master_can_book_over_the_limit_and_manual_bookings_do_not_count(session, test_user, manicure_service):
    await _hours(session)
    for hhmm in ("10:00", "12:00", "14:00", "16:00"):
        await _book(session, test_user, manicure_service, hhmm, manual_client_name="Анна")  # без лимита
    # Записи, созданные мастером вручную, не расходуют лимит клиента.
    await _book(session, test_user, manicure_service, "17:00", enforce_client_limits=True)


async def test_reschedule_is_not_blocked_by_the_limit(session, test_user, manicure_service):
    await _hours(session)
    bookings = [await _book(session, test_user, manicure_service, h, enforce_client_limits=True) for h in ("10:00", "12:00", "14:00")]
    moved = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).reschedule_booking(bookings[0].id, at("17:00"))
    await session.commit()
    assert moved.status == BookingStatus.CONFIRMED.value


async def test_restricted_client_cannot_book_but_master_still_can_book_for_him(session, test_user, manicure_service):
    await _hours(session)
    await set_client(session, test_user, is_restricted=True)
    with pytest.raises(ClientRestrictedError):
        await _book(session, test_user, manicure_service, "10:00", enforce_client_limits=True)
    await _book(session, test_user, manicure_service, "10:00", manual_client_name="Иван")


async def _cancel_via_bot(h, booking_id: int) -> None:
    await h.press(CLIENT_TG, BookingActionCallback(booking_id=booking_id, action="cancel_confirm"))


async def test_three_client_cancellations_in_a_row_alert_the_master(session, test_user, manicure_service):
    await _hours(session)
    done = await _book(session, test_user, manicure_service, "10:00")
    done.status = BookingStatus.COMPLETED.value
    ids = [(await _book(session, test_user, manicure_service, h, enforce_client_limits=True)).id for h in ("12:00", "14:00", "16:00")]
    await session.commit()

    async with tg_harness(my_booking.router) as h:
        await _cancel_via_bot(h, ids[0])
        await _cancel_via_bot(h, ids[1])
        assert not [t for t in h.tg.sent_to(ADMIN_ID) if "три раза подряд" in t]
        await _cancel_via_bot(h, ids[2])

        alerts = [t for t in h.tg.sent_to(ADMIN_ID) if "⚠️ Клиент записался три раза подряд и создал три отмены!" in t]
        assert len(alerts) == 1
        assert "@user111111" not in alerts[0]  # username берётся из БД (client1), а не из Telegram-апдейта
        assert "@client1" in alerts[0] and "Ivan" in alerts[0]
        assert "Выполненных записей: 1" in alerts[0]
        markup = next(r for r in h.tg.requests if getattr(r, "reply_markup", None) is not None and "три раза" in (r.text or "")).reply_markup
        assert [b.text for row in markup.inline_keyboard for b in row] == ["🚫 Ограничить доступ", "✅ Подтвердить доступ"]


async def test_cancel_by_master_or_reschedule_does_not_count_as_streak(session, test_user, manicure_service):
    await _hours(session)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    a = await _book(session, test_user, manicure_service, "10:00", enforce_client_limits=True)
    moved = await svc.reschedule_booking(a.id, at("11:00"))  # старая запись отменена переносом
    b = await _book(session, test_user, manicure_service, "13:00", enforce_client_limits=True)
    c = await _book(session, test_user, manicure_service, "15:00", enforce_client_limits=True)
    await session.commit()

    await svc.cancel_booking(moved.id, by_client=True)
    await svc.cancel_booking(b.id, by_client=True)
    await svc.cancel_booking(c.id)  # отменил мастер — серию рвёт
    await session.commit()
    user = await session.get(User, test_user.id)
    assert await svc.cancellation_streak(user) == 0


async def test_streak_is_broken_by_completed_booking(session, test_user, manicure_service):
    await _hours(session)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    first = await _book(session, test_user, manicure_service, "10:00", enforce_client_limits=True)
    second = await _book(session, test_user, manicure_service, "12:00", enforce_client_limits=True)
    third = await _book(session, test_user, manicure_service, "14:00", enforce_client_limits=True)
    await svc.cancel_booking(first.id, by_client=True)
    third.status = BookingStatus.COMPLETED.value  # свежая выполненная запись между отменами
    await svc.cancel_booking(second.id, by_client=True)
    await session.commit()
    assert await svc.cancellation_streak(await session.get(User, test_user.id)) == 0


async def test_master_restricts_then_restores_access(session, test_user, manicure_service):
    user_id = test_user.id
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="restrict"))
        session.expire_all()
        assert (await client_row(session, user_id)).is_restricted is True
        assert "Доступ ограничен" in h.tg.edited()[-1]

        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
        session.expire_all()
        client = await client_row(session, user_id)
        assert client.is_restricted is False
        assert client.cancel_streak_reset_at is not None
        assert "Доступ подтверждён" in h.tg.edited()[-1]


async def test_only_master_can_press_access_buttons(session, test_user):
    user_id = test_user.id
    async with tg_harness(client_access.router) as h:
        await h.press(CLIENT_TG, AdminClientAccessCallback(user_id=user_id, action="restrict"))
    session.expire_all()
    assert (await client_row(session, user_id)).is_restricted is False


async def test_allow_resets_streak_so_old_cancellations_do_not_count(session, test_user, manicure_service):
    await _hours(session)
    user_id = test_user.id
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    for h in ("10:00", "12:00", "14:00"):
        b = await _book(session, test_user, manicure_service, h, enforce_client_limits=True)
        await svc.cancel_booking(b.id, by_client=True)
    await session.commit()
    assert await svc.is_cancellation_alert(await session.get(User, user_id)) is True

    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
    session.expire_all()
    assert await svc.cancellation_streak(await session.get(User, user_id)) == 0


async def test_restrict_resets_streak_so_old_cancellations_do_not_count(session, test_user, manicure_service):
    await _hours(session)
    user_id = test_user.id
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    for h in ("10:00", "12:00", "14:00"):
        b = await _book(session, test_user, manicure_service, h, enforce_client_limits=True)
        await svc.cancel_booking(b.id, by_client=True)
    await session.commit()
    assert await svc.is_cancellation_alert(await session.get(User, user_id)) is True

    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="restrict"))
    session.expire_all()
    client = await client_row(session, user_id)
    assert client.is_restricted is True
    assert client.cancel_streak_reset_at is not None
    assert await svc.cancellation_streak(await session.get(User, user_id)) == 0


async def test_restricted_client_sees_message_instead_of_services(session, test_user, manicure_service):
    await set_client(session, test_user, is_restricted=True)
    async with tg_harness(client_booking.router) as h:
        await h.send_text(CLIENT_TG, "📅 Записаться")
        assert any("запись для вас сейчас недоступна" in t for t in h.tg.texts())
        assert not any("Выберите услугу" in t for t in h.tg.texts())


async def _book_and_cancel_all(h, session, user, service, hours):
    ids = [(await _book(session, user, service, hh, enforce_client_limits=True)).id for hh in hours]
    await session.commit()
    for booking_id in ids:
        await _cancel_via_bot(h, booking_id)


async def test_master_gets_alert_again_after_every_new_three_cancellations(session, test_user, manicure_service):
    """Мастер не нажал кнопки под предупреждением — следующая серия из трёх отмен снова должна его предупредить."""
    await _hours(session)
    alert = "⚠️ Клиент записался три раза подряд и создал три отмены!"
    async with tg_harness(my_booking.router) as h:
        await _book_and_cancel_all(h, session, test_user, manicure_service, ("10:00", "12:00", "14:00"))
        assert len([t for t in h.tg.sent_to(ADMIN_ID) if alert in t]) == 1
        await _book_and_cancel_all(h, session, test_user, manicure_service, ("10:00", "12:00", "14:00"))
        assert len([t for t in h.tg.sent_to(ADMIN_ID) if alert in t]) == 2
        await _book_and_cancel_all(h, session, test_user, manicure_service, ("10:00", "12:00", "14:00"))
        assert len([t for t in h.tg.sent_to(ADMIN_ID) if alert in t]) == 3


async def test_alert_fires_when_client_cancels_four_bookings_in_a_row(session, test_user, manicure_service):
    """Четыре отмены подряд (например, четвёртая запись по разрешению мастера) — тоже повод для предупреждения."""
    await _hours(session)
    alert = "⚠️ Клиент записался четыре раза подряд и создал четыре отмены!"
    async with tg_harness(my_booking.router) as h:
        ids = [(await _book(session, test_user, manicure_service, hh)).id for hh in ("10:00", "12:00", "14:00", "16:00")]
        await session.commit()
        for booking_id in ids:
            await _cancel_via_bot(h, booking_id)
        assert len([t for t in h.tg.sent_to(ADMIN_ID) if alert in t]) == 1


async def test_alert_shows_real_number_when_client_cancels_five_in_a_row(session, test_user, manicure_service):
    """Три записи + два разрешения мастера сверх лимита, все пять отменены: в уведомлении «пять», а не «три»."""
    await _hours(session)
    async with tg_harness(my_booking.router) as h:
        ids = [(await _book(session, test_user, manicure_service, hh)).id for hh in ("10:00", "12:00", "14:00", "16:00", "17:30")]
        await session.commit()
        for booking_id in ids:
            await _cancel_via_bot(h, booking_id)
        alerts = [t for t in h.tg.sent_to(ADMIN_ID) if "⚠️" in t]
        assert len(alerts) == 1
        assert "⚠️ Клиент записался пять раз подряд и создал пять отмен!" in alerts[0]


async def _approve_two_extra_bookings(session, user_id: int) -> None:
    """Мастер дал клиенту два разовых разрешения на запись сверх лимита."""
    for _ in range(2):
        session.add(BookingLimitRequest(master_id=MASTER_ID, user_id=user_id, message="ещё запись", status=LimitRequestStatus.APPROVED.value))
    await session.commit()


async def test_over_limit_client_cancels_any_three_of_five_and_master_is_alerted(session, test_user, manicure_service):
    """5 записей (3 + 2 по разрешению мастера), клиент отменил три ЛЮБЫХ (не обязательно последние) — мастеру приходит предупреждение."""
    await _hours(session)
    ids = [(await _book(session, test_user, manicure_service, hh, enforce_client_limits=True)).id for hh in ("10:00", "12:00", "14:00")]
    await _approve_two_extra_bookings(session, test_user.id)  # лимит достигнут — мастер разрешает ещё две
    ids += [(await _book(session, test_user, manicure_service, hh, enforce_client_limits=True)).id for hh in ("16:00", "17:30")]

    async with tg_harness(my_booking.router) as h:
        await _cancel_via_bot(h, ids[0])
        await _cancel_via_bot(h, ids[1])
        assert not [t for t in h.tg.sent_to(ADMIN_ID) if "⚠️" in t]
        await _cancel_via_bot(h, ids[2])  # самая новая запись (5-я) осталась действующей, но это всё равно три отмены

        alerts = [t for t in h.tg.sent_to(ADMIN_ID) if "⚠️" in t]
        assert len(alerts) == 1
        assert "⚠️ Клиент отменил три записи, хотя у него было больше трёх записей!" in alerts[0]
        assert "@client1" in alerts[0]


async def test_three_scattered_cancellations_without_extra_bookings_do_not_alert(session, test_user, manicure_service):
    """Старое правило не тронуто: без записей сверх лимита три отмены не подряд предупреждения не вызывают."""
    await _hours(session)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    a = await _book(session, test_user, manicure_service, "10:00", enforce_client_limits=True)
    await svc.cancel_booking(a.id, by_client=True)
    keep = await _book(session, test_user, manicure_service, "12:00", enforce_client_limits=True)
    b = await _book(session, test_user, manicure_service, "14:00", enforce_client_limits=True)
    c = await _book(session, test_user, manicure_service, "16:00", enforce_client_limits=True)
    await session.commit()
    async with tg_harness(my_booking.router) as h:
        await _cancel_via_bot(h, b.id)
        await _cancel_via_bot(h, c.id)
        assert not [t for t in h.tg.sent_to(ADMIN_ID) if "⚠️" in t]
    assert keep.id


async def test_restrict_cancels_remaining_bookings_and_notifies_client_and_master(session, test_user, manicure_service):
    await _hours(session)
    user_id = test_user.id
    service_id = manicure_service.id
    first = await _book(session, test_user, manicure_service, "10:00", enforce_client_limits=True)
    second = await _book(session, test_user, manicure_service, "12:00", enforce_client_limits=True)
    ids = (first.id, second.id)

    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="restrict"))
        session.expire_all()
        assert (await client_row(session, user_id)).is_restricted is True
        for booking_id in ids:
            booking = await session.get(Booking, booking_id)
            assert booking.status == BookingStatus.CANCELLED.value
            assert booking.cancelled_by_client is False  # отмена от мастера, в серию клиента не идёт

        to_client = h.tg.sent_to(CLIENT_TG)
        assert len(to_client) == 1 and "ограничил вам доступ" in to_client[0] and "записи отменены" in to_client[0]
        assert to_client[0].count("Маникюр") == 2
        to_master = [t for t in h.tg.sent_to(ADMIN_ID) if "Доступ клиента ограничен" in t]
        assert len(to_master) == 1 and "Отменено записей: 2" in to_master[0] and "@client1" in to_master[0]

        # Повторное нажатие кнопки не плодит сообщения.
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="restrict"))
        assert len(h.tg.sent_to(CLIENT_TG)) == 1
        assert len([t for t in h.tg.sent_to(ADMIN_ID) if "Доступ клиента ограничен" in t]) == 1

    # Места освободились: на эти слоты снова можно записать.
    user = await session.get(User, user_id)
    service = await session.get(Service, service_id)
    await _book(session, user, service, "10:00", manual_client_name="Анна")


async def test_restrict_without_bookings_still_tells_client_about_restriction(session, test_user, manicure_service):
    user_id = test_user.id
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="restrict"))
        assert any("ограничил вам доступ" in t for t in h.tg.sent_to(CLIENT_TG))
        assert any("Действующих записей у клиента не было" in t for t in h.tg.sent_to(ADMIN_ID))


async def test_burned_approval_does_not_count_as_over_limit(session, test_user, manicure_service):
    """Неиспользованное разрешение «сгорело», когда у клиента было меньше трёх записей: больше трёх записей не было,
    три разрозненные отмены предупреждения вызывать не должны."""
    await _hours(session)
    user_id = test_user.id
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    session.add(BookingLimitRequest(master_id=MASTER_ID, user_id=user_id, message="на всякий", status=LimitRequestStatus.APPROVED.value))
    await session.commit()
    a = await _book(session, test_user, manicure_service, "10:00", enforce_client_limits=True)  # здесь разрешение сгорает
    await svc.cancel_booking(a.id, by_client=True)
    keep = await _book(session, test_user, manicure_service, "12:00", enforce_client_limits=True)
    b = await _book(session, test_user, manicure_service, "14:00", enforce_client_limits=True)
    c = await _book(session, test_user, manicure_service, "16:00", enforce_client_limits=True)
    keep_id = keep.id
    async with tg_harness(my_booking.router) as h:
        await _cancel_via_bot(h, b.id)
        await _cancel_via_bot(h, c.id)
        assert not [t for t in h.tg.sent_to(ADMIN_ID) if "⚠️" in t]
    session.expire_all()
    assert (await client_row(session, user_id)).over_limit_cancels is None
    assert keep_id


async def test_over_limit_state_ends_when_client_is_back_under_the_limit(session, test_user, manicure_service):
    """Было пять записей, три выполнены, клиент отменил одну из двух оставшихся: счёт снят, старые отмены не копятся."""
    await _hours(session)
    user_id = test_user.id
    ids = [(await _book(session, test_user, manicure_service, hh, enforce_client_limits=True)).id for hh in ("10:00", "12:00", "14:00")]
    await _approve_two_extra_bookings(session, user_id)
    ids += [(await _book(session, test_user, manicure_service, hh, enforce_client_limits=True)).id for hh in ("16:00", "17:30")]
    session.expire_all()
    assert (await client_row(session, user_id)).over_limit_cancels == 0  # состояние «больше трёх» началось

    for booking_id in ids[:3]:
        (await session.get(Booking, booking_id)).status = BookingStatus.COMPLETED.value
    await session.commit()
    async with tg_harness(my_booking.router) as h:
        await _cancel_via_bot(h, ids[3])
        assert not [t for t in h.tg.sent_to(ADMIN_ID) if "⚠️" in t]
    session.expire_all()
    assert (await client_row(session, user_id)).over_limit_cancels is None


async def test_alert_and_master_decisions_clear_over_limit_counter(session, test_user, manicure_service):
    await _hours(session)
    user_id = test_user.id
    ids = [(await _book(session, test_user, manicure_service, hh, enforce_client_limits=True)).id for hh in ("10:00", "12:00", "14:00")]
    await _approve_two_extra_bookings(session, user_id)
    ids += [(await _book(session, test_user, manicure_service, hh, enforce_client_limits=True)).id for hh in ("16:00", "17:30")]
    async with tg_harness(my_booking.router) as h:
        for booking_id in ids[:3]:
            await _cancel_via_bot(h, booking_id)
    session.expire_all()
    client = await client_row(session, user_id)
    assert client.over_limit_cancels is None and client.cancel_streak_reset_at is not None  # после предупреждения — с нуля

    await set_client(session, user_id, over_limit_cancels=2)
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
    session.expire_all()
    assert (await client_row(session, user_id)).over_limit_cancels is None


async def test_allow_notifies_restricted_client_that_access_is_back(session, test_user):
    user_id = test_user.id
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="restrict"))
        before = len(h.tg.sent_to(CLIENT_TG))
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
        assert h.tg.sent_to(CLIENT_TG)[before:] == [OPENED_TEXT]
        assert h.tg.alerts() == []

        # Повторное нажатие (кнопка в старом сообщении): доступ уже открыт, лишнего сообщения нет.
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
        assert h.tg.sent_to(CLIENT_TG)[before:] == [OPENED_TEXT]


async def test_allow_notifies_client_blocked_forever(session, test_user):
    user_id = test_user.id
    await set_client(session, test_user, is_restricted=True, is_blocked_forever=True)
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
        assert h.tg.sent_to(CLIENT_TG) == [OPENED_TEXT]
    session.expire_all()
    client = await client_row(session, user_id)
    assert client.is_restricted is False and client.is_blocked_forever is False


async def test_allow_does_not_message_client_who_was_never_restricted(session, test_user):
    """«Подтвердить» под предупреждением об отменах: клиент не ограничен, «мастер открыл доступ» было бы неправдой."""
    user_id = test_user.id
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
        assert h.tg.sent_to(CLIENT_TG) == []
        assert "Доступ подтверждён" in h.tg.edited()[-1]
        assert h.tg.alerts() == []


async def test_allow_warns_master_when_client_cannot_be_notified(session, test_user):
    user_id = test_user.id
    await set_client(session, test_user, is_restricted=True)
    async with tg_harness(client_access.router) as h:
        h.tg.blocked_chat_ids.add(CLIENT_TG)
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
        assert h.tg.alerts() == ["Доступ подтверждён, но клиенту доставить сообщение не удалось."]
    session.expire_all()
    assert (await client_row(session, user_id)).is_restricted is False  # решение мастера сохранено

