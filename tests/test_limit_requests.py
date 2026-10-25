"""Запрос клиента к мастеру на запись сверх лимита: клиент пишет → мастер принимает/отклоняет → разовое разрешение."""

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.admin import limit_requests as admin_limit_requests
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import master_request
from app.bot.handlers.client import content as client_content
from app.bot.keyboards.callback_data import (
    AdminLimitCommentCallback,
    AdminLimitRequestCallback,
    MasterRequestCallback,
)
from app.bot.states.booking_states import MasterReplyFSM, MasterRequestFSM
from app.database.models import BookingLimitRequest, LimitRequestStatus, WorkingHours
from app.services.booking_service import BookingService
from app.services.exceptions import BookingLimitReachedError
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


async def _full_client(session, user, service):
    """Клиент с тремя активными записями (лимит достигнут)."""
    session.add(WorkingHours(master_id=MASTER_ID, weekday=DAY.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    await session.commit()
    for hhmm in ("10:00", "12:00", "14:00"):
        await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(user.id, service.id, at(hhmm), enforce_client_limits=True)
    await session.commit()


async def _book(session, user, service, hhmm):
    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(
        user.id, service.id, at(hhmm), enforce_client_limits=True
    )
    await session.commit()
    return booking


async def _only_request(session) -> BookingLimitRequest:
    result = await session.execute(select(BookingLimitRequest).execution_options(populate_existing=True))
    return result.scalars().one()


async def _send_request(h, text="Пожалуйста, можно ещё одну запись?"):
    await h.press(CLIENT_TG, MasterRequestCallback(action="write"))
    await h.send_text(CLIENT_TG, text)


async def test_limit_message_has_button_and_new_text(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(client_booking.router) as h:
        await h.send_text(CLIENT_TG, "📅 Записаться")
        sent = next(r for r in h.tg.requests if "3 активные записи" in (getattr(r, "text", "") or ""))
        assert "Также вы можете обратиться к мастеру" in sent.text
        buttons = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
        assert buttons == ["✉️ Написать мастеру"]


async def test_full_flow_approve_with_comment_gives_one_extra_booking(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router, admin_limit_requests.router) as h:
        await _send_request(h)
        assert await h.get_state(CLIENT_TG) is None
        assert any("Сообщение передано мастеру" in t for t in h.tg.sent_to(CLIENT_TG))
        req = await _only_request(session)
        assert req.status == LimitRequestStatus.PENDING.value
        to_master = h.tg.sent_to(ADMIN_ID)[-1]
        assert "Пожалуйста, можно ещё одну запись?" in to_master and "@client1" in to_master

        await h.press(ADMIN_ID, AdminLimitRequestCallback(request_id=req.id, action="approve"))
        assert "Желаете добавить сообщение?" in h.tg.sent_to(ADMIN_ID)
        assert not any("принял" in t for t in h.tg.sent_to(CLIENT_TG))  # клиент ждёт, пока мастер решит про сообщение

        await h.press(ADMIN_ID, AdminLimitCommentCallback(request_id=req.id, action="yes"))
        assert await h.get_state(ADMIN_ID) == MasterReplyFSM.waiting_text.state
        await h.send_text(ADMIN_ID, "Приходите в пятницу")
        assert await h.get_state(ADMIN_ID) is None
        client_texts = h.tg.sent_to(CLIENT_TG)
        assert "Мастер принял ваш запрос" in client_texts[-1] and "Приходите в пятницу" in client_texts[-1]

    req = await _only_request(session)
    assert req.status == LimitRequestStatus.APPROVED.value and req.client_notified_at is not None

    await _book(session, test_user, manicure_service, "16:00")  # сверх лимита — разрешено
    assert (await _only_request(session)).override_used_at is not None
    with pytest.raises(BookingLimitReachedError):  # разрешение разовое
        await _book(session, test_user, manicure_service, "17:00")


async def test_reject_without_comment_sends_only_decision(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router, admin_limit_requests.router) as h:
        await _send_request(h)
        req = await _only_request(session)
        await h.press(ADMIN_ID, AdminLimitRequestCallback(request_id=req.id, action="reject"))
        await h.press(ADMIN_ID, AdminLimitCommentCallback(request_id=req.id, action="no"))
        last = h.tg.sent_to(CLIENT_TG)[-1]
        assert "Мастер отклонил ваш запрос" in last and "Сообщение от мастера" not in last
    assert (await _only_request(session)).status == LimitRequestStatus.REJECTED.value
    with pytest.raises(BookingLimitReachedError):
        await _book(session, test_user, manicure_service, "16:00")


async def test_second_press_while_pending_shows_waiting_alert(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router, admin_limit_requests.router) as h:
        await _send_request(h)
        await h.press(CLIENT_TG, MasterRequestCallback(action="write"))
        assert h.tg.alerts() == ["Ваш запрос уже отправлен мастеру. Пожалуйста, дождитесь его решения."]
        assert await h.get_state(CLIENT_TG) is None
    assert len((await session.execute(select(BookingLimitRequest))).scalars().all()) == 1


async def test_double_press_on_master_buttons_is_harmless(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router, admin_limit_requests.router) as h:
        await _send_request(h)
        req = await _only_request(session)
        await h.press(ADMIN_ID, AdminLimitRequestCallback(request_id=req.id, action="approve"))
        await h.press(ADMIN_ID, AdminLimitRequestCallback(request_id=req.id, action="reject"))
        assert h.tg.alerts() == ["Этот запрос уже обработан."]
        await h.press(ADMIN_ID, AdminLimitCommentCallback(request_id=req.id, action="no"))
        await h.press(ADMIN_ID, AdminLimitCommentCallback(request_id=req.id, action="no"))
        assert len([t for t in h.tg.sent_to(CLIENT_TG) if "Мастер принял" in t]) == 1  # клиенту — один раз
    assert (await _only_request(session)).status == LimitRequestStatus.APPROVED.value


async def test_only_master_can_decide(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router, admin_limit_requests.router) as h:
        await _send_request(h)
        req = await _only_request(session)
        await h.press(CLIENT_TG, AdminLimitRequestCallback(request_id=req.id, action="approve"))
    assert (await _only_request(session)).status == LimitRequestStatus.PENDING.value


async def test_too_long_or_non_text_message_is_not_sent(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router) as h:
        await h.press(CLIENT_TG, MasterRequestCallback(action="write"))
        await h.send_text(CLIENT_TG, "а" * 501)
        assert any("слишком длинное" in t for t in h.tg.sent_to(CLIENT_TG))
        assert await h.get_state(CLIENT_TG) == MasterRequestFSM.waiting_message.state
        assert h.tg.sent_to(ADMIN_ID) == []
    assert (await session.execute(select(BookingLimitRequest))).scalars().all() == []


async def test_client_can_cancel_writing(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router) as h:
        await h.press(CLIENT_TG, MasterRequestCallback(action="write"))
        assert await h.get_state(CLIENT_TG) == MasterRequestFSM.waiting_message.state
        await h.press(CLIENT_TG, MasterRequestCallback(action="cancel"))
        assert await h.get_state(CLIENT_TG) is None


async def test_menu_button_interrupts_client_input(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router, client_content.router, reset_input=True) as h:
        await h.press(CLIENT_TG, MasterRequestCallback(action="write"))
        await h.send_text(CLIENT_TG, "💰 Услуги и цены")
        assert await h.get_state(CLIENT_TG) is None
        assert h.tg.sent_to(ADMIN_ID) == []
    assert (await session.execute(select(BookingLimitRequest))).scalars().all() == []


async def test_master_menu_button_still_delivers_decision_without_text(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router, admin_limit_requests.router, client_content.router, reset_input=True) as h:
        await _send_request(h)
        req = await _only_request(session)
        await h.press(ADMIN_ID, AdminLimitRequestCallback(request_id=req.id, action="approve"))
        await h.press(ADMIN_ID, AdminLimitCommentCallback(request_id=req.id, action="yes"))
        await h.send_text(ADMIN_ID, "💰 Услуги и цены")  # передумал писать
        assert await h.get_state(ADMIN_ID) is None
        assert "Мастер принял ваш запрос" in h.tg.sent_to(CLIENT_TG)[-1]


async def test_request_is_dropped_if_master_cannot_be_reached(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    async with tg_harness(master_request.router) as h:
        h.tg.blocked_chat_ids.add(ADMIN_ID)
        await _send_request(h)
        assert any("Не удалось передать" in t for t in h.tg.sent_to(CLIENT_TG))
    assert (await session.execute(select(BookingLimitRequest))).scalars().all() == []


async def test_button_does_nothing_when_a_place_is_already_free(session, test_user, manicure_service):
    async with tg_harness(master_request.router) as h:
        await h.press(CLIENT_TG, MasterRequestCallback(action="write"))
        assert h.tg.alerts() == ["Вы уже можете записаться: нажмите «📅 Записаться»."]
        assert await h.get_state(CLIENT_TG) is None


async def _approved_unused(session, user):
    from app.database.models import BookingLimitRequest

    session.add(BookingLimitRequest(master_id=MASTER_ID, user_id=user.id, message="ещё одну", status=LimitRequestStatus.APPROVED.value))
    await session.commit()


async def test_approval_burns_when_master_restricts_client(session, test_user, manicure_service):
    from app.bot.handlers.admin import client_access
    from app.bot.keyboards.callback_data import AdminClientAccessCallback

    user_id = test_user.id
    await _full_client(session, test_user, manicure_service)
    await _approved_unused(session, test_user)
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="restrict"))
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
    assert (await _only_request(session)).override_used_at is not None
    # Ограничение отменило прежние записи клиента; после разблокировки — снова обычный лимит в 3 записи.
    for hhmm in ("10:00", "12:00", "14:00"):
        await _book(session, test_user, manicure_service, hhmm)
    with pytest.raises(BookingLimitReachedError):
        await _book(session, test_user, manicure_service, "16:00")


async def test_approval_burns_when_master_blocks_forever(session, test_user, manicure_service):
    from app.bot.handlers.admin import access_requests
    from app.bot.keyboards.callback_data import AdminAccessRequestCallback
    from app.database.models import ClientAccessRequest

    user_id = test_user.id
    await _full_client(session, test_user, manicure_service)
    await _approved_unused(session, test_user)
    await set_client(session, test_user, is_restricted=True)
    session.add(ClientAccessRequest(master_id=MASTER_ID, user_id=user_id, message="простите"))
    await session.commit()
    access_id = (await session.execute(select(ClientAccessRequest.id))).scalar_one()
    async with tg_harness(access_requests.router) as h:
        await h.press(ADMIN_ID, AdminAccessRequestCallback(request_id=access_id, action="block"))
        await h.press(ADMIN_ID, AdminAccessRequestCallback(request_id=access_id, action="open"))  # уже обработано
    from app.bot.handlers.admin import client_access
    from app.bot.keyboards.callback_data import AdminClientAccessCallback

    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))  # снял блокировку
    result = await session.execute(
        select(BookingLimitRequest).execution_options(populate_existing=True)
    )
    assert result.scalars().one().override_used_at is not None
    client = await client_row(session, test_user)  # флаги меняли обработчики в других сессиях
    assert client.is_restricted is False and client.is_blocked_forever is False
    with pytest.raises(BookingLimitReachedError):
        await _book(session, test_user, manicure_service, "16:00")


# --- реальное число активных записей и «правило заново» после выполнения/отмены ---


@pytest.mark.parametrize(
    "count, expected",
    [(1, "1 активная запись"), (3, "3 активные записи"), (4, "4 активные записи"), (5, "5 активных записей"),
     (11, "11 активных записей"), (21, "21 активная запись")],
)
def test_limit_text_names_the_real_number_of_bookings(count, expected):
    from app.bot.handlers.client.booking import limit_reached_text

    assert limit_reached_text(count).startswith(f"⚠️ У вас уже {expected} — это максимум.")


async def test_limit_message_shows_actual_count_after_extra_booking(session, test_user, manicure_service):
    from app.bot.handlers.client import booking as client_booking

    await _full_client(session, test_user, manicure_service)
    await _approved_unused(session, test_user)
    await _book(session, test_user, manicure_service, "16:00")  # четвёртая — по разрешению
    async with tg_harness(client_booking.router) as h:
        await h.send_text(CLIENT_TG, "📅 Записаться")
        text = next(t for t in h.tg.sent_to(CLIENT_TG) if "активные записи" in t)
        assert text.startswith("⚠️ У вас уже 4 активные записи — это максимум.")


@pytest.mark.parametrize(
    "count, by_master, self_phrase, master_phrase",
    [
        (3, 2, "3 записи, сделанные самостоятельно", "Ещё 2 записи создал мастер"),
        (3, 1, "3 записи, сделанные самостоятельно", "Ещё 1 запись создал мастер"),
        (3, 5, "3 записи, сделанные самостоятельно", "Ещё 5 записей создал мастер"),
        (4, 2, "4 записи, сделанные самостоятельно", "Ещё 2 записи создал мастер"),
        (5, 11, "5 записей, сделанных самостоятельно", "Ещё 11 записей создал мастер"),
        (21, 21, "21 запись, сделанная самостоятельно", "Ещё 21 запись создал мастер"),
    ],
)
def test_limit_text_explains_bookings_created_by_master(count, by_master, self_phrase, master_phrase):
    from app.bot.handlers.client.booking import limit_reached_text

    text = limit_reached_text(count, by_master)
    assert text.startswith(f"⚠️ У вас уже {self_phrase} — это максимум.\n\n{master_phrase} — они не учитываются в лимите.")
    assert "Дождитесь визита" in text and "обратиться к мастеру" in text


def test_limit_text_without_master_bookings_is_unchanged():
    from app.bot.handlers.client.booking import limit_reached_text

    assert limit_reached_text(3, 0) == limit_reached_text(3) == limit_reached_text()
    assert "создал мастер" not in limit_reached_text(3, 0)


async def _master_created_for_client(session, user, service, hhmm):
    """Запись, которую мастер создал вручную и которая привязана к самому клиенту (по @username или ссылке)."""
    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(
        user.id, service.id, at(hhmm), manual_client_name="Анна", manual_client_contact="@client"
    )
    await session.commit()
    return booking


async def test_limit_error_reports_master_created_bookings_separately(session, test_user, manicure_service):
    await _full_client(session, test_user, manicure_service)
    await _master_created_for_client(session, test_user, manicure_service, "16:00")
    await _master_created_for_client(session, test_user, manicure_service, "17:00")

    with pytest.raises(BookingLimitReachedError) as exc_info:
        await BookingService(session, RIGA_TZ, master_id=MASTER_ID).ensure_client_can_book(test_user.id)

    assert exc_info.value.active == 3  # лимит по-прежнему считает только самостоятельные записи
    assert exc_info.value.by_master == 2


async def test_master_created_bookings_still_do_not_count_towards_limit(session, test_user, manicure_service):
    """Логика лимита не менялась: 2 самостоятельные + 2 от мастера — клиент ещё может записаться."""
    session.add(WorkingHours(master_id=MASTER_ID, weekday=DAY.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    await session.commit()
    await _book(session, test_user, manicure_service, "10:00")
    await _book(session, test_user, manicure_service, "12:00")
    await _master_created_for_client(session, test_user, manicure_service, "14:00")
    await _master_created_for_client(session, test_user, manicure_service, "16:00")

    assert await BookingService(session, RIGA_TZ, master_id=MASTER_ID).ensure_client_can_book(test_user.id) is None


async def test_limit_message_mentions_master_created_bookings(session, test_user, manicure_service):
    from app.bot.handlers.client import booking as client_booking

    await _full_client(session, test_user, manicure_service)
    await _master_created_for_client(session, test_user, manicure_service, "16:00")
    await _master_created_for_client(session, test_user, manicure_service, "17:00")
    async with tg_harness(client_booking.router) as h:
        await h.send_text(CLIENT_TG, "📅 Записаться")
        text = next(t for t in h.tg.sent_to(CLIENT_TG) if "это максимум" in t)
        assert text.startswith(
            "⚠️ У вас уже 3 записи, сделанные самостоятельно — это максимум.\n\n"
            "Ещё 2 записи создал мастер — они не учитываются в лимите."
        )


async def test_rule_starts_over_after_a_booking_is_cancelled_or_done(session, test_user, manicure_service):
    """У клиента 3 записи и неиспользованное разрешение. Одна запись отменена → записался обычным образом →
    снова три записи, а старое разрешение уже не действует: нужно новое."""
    await _full_client(session, test_user, manicure_service)
    await _approved_unused(session, test_user)
    from app.database.models import Booking

    first = (await session.execute(select(Booking).where(Booking.user_id == test_user.id).order_by(Booking.id))).scalars().first()
    await BookingService(session, RIGA_TZ, master_id=MASTER_ID).cancel_booking(first.id, by_client=True)
    await session.commit()

    await _book(session, test_user, manicure_service, "16:00")  # место освободилось — обычная запись
    assert (await _only_request(session)).override_used_at is not None  # разрешение сгорело
    with pytest.raises(BookingLimitReachedError):
        await _book(session, test_user, manicure_service, "17:00")
