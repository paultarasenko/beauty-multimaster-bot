"""Воркер напоминаний: фабрики отправки, тик планировщика и перевод прошедших записей в «выполнена».

Раньше scheduler/reminder_worker.py и complete_past_bookings не были покрыты вообще, а именно здесь живут
тексты напоминаний, которые клиент и мастер получают в Telegram.
"""

import datetime as dt
import re

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.methods import SendMessage
from sqlalchemy import select, text

from app.database.models import (
    Booking,
    BookingStatus,
    MasterProfile,
    Notification,
    NotificationStatus,
    NotificationType,
    Service,
    User,
    WorkingHours,
)
from app.database.session import make_session_factory
from app.scheduler.reminder_worker import (
    make_send_admin_reminder_func,
    make_send_client_reminder_2h_func,
    make_send_master_reminder_2h_func,
    make_send_reminder_func,
    start_reminder_scheduler,
)
from app.services.booking_service import BookingService, complete_past_bookings
from app.services.reminder_service import ReminderService
from tests.conftest import MASTER_ID, RIGA_TZ, TEST_DATABASE_URL, next_weekday, set_owner
from tests.tg import BOT_ID, RecordingSession

ADMIN_ID = 999
CLIENT_TG = 111111  # test_user
HOSTILE_SERVICE = "Брови & <b>ресницы</b>"


def _assert_safe_html(value: str) -> None:
    assert "<" not in value and ">" not in value, value
    assert not re.search(r"&(?!(?:amp|lt|gt|quot|#x?\d+);)", value), value


@pytest.fixture
def tg_bot():
    tg = RecordingSession()
    return Bot(token=f"{BOT_ID}:TEST", session=tg), tg


@pytest.fixture
def session_factory():
    return make_session_factory(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def future_booking(session, test_user) -> Booking:
    """Запись клиента на пятницу через 2+ недели; услуга и мастер с «враждебными» именами."""
    await set_owner(session, ADMIN_ID)
    session.add(MasterProfile(master_id=MASTER_ID, name="Мария&", timezone="Europe/Riga", address="ул. <Ленина> & 5"))
    day = next_weekday(4)
    session.add(WorkingHours(master_id=MASTER_ID, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    service = Service(master_id=MASTER_ID, name=HOSTILE_SERVICE, price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)
    await session.commit()
    start = dt.datetime.combine(day, dt.time(15, 0), tzinfo=RIGA_TZ)
    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(test_user.id, service.id, start)
    await session.commit()
    return booking


def _sent(tg: RecordingSession) -> list[SendMessage]:
    return [m for m in tg.requests if isinstance(m, SendMessage)]


# --- фабрики отправки ---


async def test_24h_reminder_goes_to_client_with_buttons_and_escaped_text(future_booking, tg_bot, session_factory):
    bot, tg = tg_bot
    await make_send_reminder_func(bot, session_factory, MASTER_ID)(future_booking)

    [msg] = _sent(tg)
    assert msg.chat_id == CLIENT_TG
    _assert_safe_html(msg.text)
    assert "Брови &amp; &lt;b&gt;ресницы&lt;/b&gt;" in msg.text and "ул. &lt;Ленина&gt; &amp; 5" in msg.text
    # Стоимость — строкой сразу после времени (услуга в фикстуре стоит 40).
    assert re.search(r"🕐 \d\d:\d\d - \d\d:\d\d\n💰 40 BYN\nул\.", msg.text), msg.text
    buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["✅ Всё в силе", "🔄 Перенести", "❌ Отменить"]
    assert all(f":{future_booking.id}:" in b.callback_data for b in buttons)


async def test_client_2h_reminder_is_escaped(future_booking, tg_bot, session_factory):
    bot, tg = tg_bot
    await make_send_client_reminder_2h_func(bot, session_factory, MASTER_ID)(future_booking)

    [msg] = _sent(tg)
    assert msg.chat_id == CLIENT_TG
    _assert_safe_html(msg.text)
    # Имя мастера в напоминании за 2 часа не показывается (см. client_reminder_2h_text); экранируется только услуга.
    assert "Брови &amp; &lt;b&gt;ресницы&lt;/b&gt;" in msg.text


async def test_master_2h_reminder_goes_to_master_and_is_escaped(session, future_booking, tg_bot, session_factory):
    bot, tg = tg_bot
    client = await session.get(User, future_booking.user_id)
    client.first_name = "<3 Anna"
    client.username = None
    await session.commit()

    await make_send_master_reminder_2h_func(bot, session_factory, MASTER_ID)(future_booking)

    [msg] = _sent(tg)
    assert msg.chat_id == ADMIN_ID
    _assert_safe_html(msg.text)
    assert "&lt;3 Anna" in msg.text and "Брови &amp; &lt;b&gt;ресницы&lt;/b&gt;" in msg.text


async def test_2h_reminders_are_skipped_if_booking_already_started(future_booking, tg_bot, session_factory):
    """Воркер простоял и опоздал: «через 2 часа» уже неправда."""
    bot, tg = tg_bot
    late = Booking(master_id=MASTER_ID,
        id=future_booking.id,
        user_id=future_booking.user_id,
        service_name_snapshot="Маникюр",
        service_price_snapshot=40,
        service_duration_snapshot=60,
        start_datetime=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        end_datetime=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=55),
    )
    await make_send_client_reminder_2h_func(bot, session_factory, MASTER_ID)(late)
    await make_send_master_reminder_2h_func(bot, session_factory, MASTER_ID)(late)
    assert _sent(tg) == []


async def test_master_2h_reminder_is_skipped_without_owner(session, future_booking, tg_bot, session_factory):
    bot, tg = tg_bot
    await set_owner(session, None)
    await make_send_master_reminder_2h_func(bot, session_factory, MASTER_ID)(future_booking)
    assert _sent(tg) == []


async def test_admin_personal_reminder_text_is_escaped(tg_bot):
    bot, tg = tg_bot
    await make_send_admin_reminder_func(bot, ADMIN_ID)("Позвонить <Ане> & Оле")

    [msg] = _sent(tg)
    assert msg.chat_id == ADMIN_ID and "Позвонить &lt;Ане&gt; &amp; Оле" in msg.text


# --- ReminderService: ветки отправки ---


async def _pending(session, booking_id: int, kind: NotificationType) -> Notification:
    session.expire_all()
    return (
        await session.execute(
            select(Notification).where(Notification.booking_id == booking_id, Notification.type == kind.value)
        )
    ).scalar_one()


async def test_reminder_not_sent_if_booking_status_changed_without_cancel_flow(session, future_booking):
    """Запись могли отменить в обход BookingService (скрипт, правка в БД) — напоминание всё равно не уходит."""
    booking_id, due = future_booking.id, future_booking.start_datetime
    await session.execute(text("UPDATE bookings SET status = 'cancelled' WHERE id = :id"), {"id": booking_id})
    await session.commit()
    session.expire_all()  # сырой UPDATE мимо ORM: сбрасываем кэш сессии (воркер в проде открывает свежую сессию на тик)
    sent: list = []

    async def send(b):
        sent.append(b.id)

    assert await ReminderService(session, send, master_id=MASTER_ID).process_due_notifications(now=due) == 0
    assert sent == []


async def test_failed_send_marks_failed_without_blocking_others_and_is_not_retried(session, future_booking):
    """Фиксируем текущую семантику: «не больше одного раза». FAILED — финальный статус, повторов нет.

    Это осознанный выбор в пользу «не прислать дважды», но он значит, что любая ошибка отправки (например,
    невалидный HTML) теряет напоминание навсегда. Меняете на at-least-once — меняйте и этот тест.
    """
    booking_id, due = future_booking.id, future_booking.start_datetime
    calls: list[str] = []

    async def send_default(b):
        calls.append("24h")
        raise RuntimeError("telegram is down")

    async def send_2h(b):
        calls.append("2h")

    service = ReminderService(
        session,
        send_default,
        send_funcs_by_type={
            NotificationType.REMINDER_2H_CLIENT.value: send_2h,
            NotificationType.REMINDER_2H_MASTER.value: send_2h,
        },
        master_id=MASTER_ID,
    )
    assert await service.process_due_notifications(now=due) == 2  # 24h упало, два «за 2 часа» ушли
    assert sorted(calls) == sorted(["24h", "2h", "2h"])
    assert (await _pending(session, booking_id, NotificationType.REMINDER_24H)).status == NotificationStatus.FAILED.value

    assert await service.process_due_notifications(now=due + dt.timedelta(minutes=1)) == 0
    assert calls.count("24h") == 1


# --- тик планировщика целиком ---


async def test_scheduler_registers_all_workers_and_tick_delivers_due_reminder(
    session, future_booking, tg_bot, session_factory
):
    bot, tg = tg_bot
    booking_id = future_booking.id
    await session.execute(
        text("UPDATE notifications SET scheduled_at = now() - interval '1 minute' WHERE type = 'reminder_24h'")
    )
    await session.commit()

    scheduler = start_reminder_scheduler(bot, session_factory, MASTER_ID)
    try:
        assert {j.id for j in scheduler.get_jobs()} == {
            "reminder_worker",
            "admin_reminder_worker",
            "booking_completion_worker",
        }
        await scheduler.get_job("reminder_worker").func()
    finally:
        scheduler.shutdown(wait=False)

    [msg] = _sent(tg)
    assert msg.chat_id == CLIENT_TG and "Напоминание о записи" in msg.text
    assert (await _pending(session, booking_id, NotificationType.REMINDER_24H)).status == NotificationStatus.SENT.value
    assert (await _pending(session, booking_id, NotificationType.REMINDER_2H_CLIENT)).status == NotificationStatus.PENDING.value


async def test_scheduler_tick_survives_telegram_failure(session, future_booking, tg_bot, session_factory):
    """Воркер не должен падать из-за одного недоставленного сообщения: исключение логируется, статус — FAILED."""
    bot, tg = tg_bot
    tg.blocked_chat_ids.add(CLIENT_TG)
    booking_id = future_booking.id
    await session.execute(
        text("UPDATE notifications SET scheduled_at = now() - interval '1 minute' WHERE type = 'reminder_24h'")
    )
    await session.commit()

    scheduler = start_reminder_scheduler(bot, session_factory, MASTER_ID)
    try:
        await scheduler.get_job("reminder_worker").func()  # не бросает
    finally:
        scheduler.shutdown(wait=False)

    assert (await _pending(session, booking_id, NotificationType.REMINDER_24H)).status == NotificationStatus.FAILED.value


async def test_scheduler_completion_tick_marks_finished_booking_completed(
    session, master_profile, test_user, manicure_service, tg_bot, session_factory
):
    bot, _ = tg_bot
    now = dt.datetime.now(dt.timezone.utc)
    session.add(
        Booking(master_id=MASTER_ID,
            user_id=test_user.id,
            service_id=manicure_service.id,
            start_datetime=now - dt.timedelta(hours=3),
            end_datetime=now - dt.timedelta(hours=2),
            status=BookingStatus.CONFIRMED.value,
            service_name_snapshot="Маникюр",
            service_price_snapshot=40,
            service_duration_snapshot=60,
        )
    )
    await session.commit()

    scheduler = start_reminder_scheduler(bot, session_factory, MASTER_ID)
    try:
        await scheduler.get_job("booking_completion_worker").func()
    finally:
        scheduler.shutdown(wait=False)

    session.expire_all()
    assert (await session.execute(select(Booking.status))).scalar_one() == BookingStatus.COMPLETED.value


# --- complete_past_bookings ---

NOW = dt.datetime(2030, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


def _booking(user_id, service_id, start_h, end_h, status) -> Booking:
    base = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    return Booking(master_id=MASTER_ID,
        user_id=user_id,
        service_id=service_id,
        start_datetime=base + dt.timedelta(hours=start_h),
        end_datetime=base + dt.timedelta(hours=end_h),
        status=status,
        service_name_snapshot="Маникюр",
        service_price_snapshot=40,
        service_duration_snapshot=60,
    )


async def test_complete_past_bookings_touches_only_finished_confirmed_ones_and_is_idempotent(
    session, test_user, manicure_service
):
    uid, sid = test_user.id, manicure_service.id
    spec = {  # старт (часы от полуночи): (конец, статус до, статус после)
        9: (10, "confirmed", "completed"),
        11: (13, "confirmed", "confirmed"),  # идёт прямо сейчас
        14: (15, "confirmed", "confirmed"),  # ещё впереди
        8: (9, "cancelled", "cancelled"),
        6: (7, "no_show", "no_show"),
        4: (5, "completed", "completed"),
    }
    session.add_all(_booking(uid, sid, start, end, before) for start, (end, before, _) in spec.items())
    await session.commit()

    assert await complete_past_bookings(session, MASTER_ID, NOW) == 1
    assert await complete_past_bookings(session, MASTER_ID, NOW) == 0  # повторный запуск ничего не меняет

    session.expire_all()
    rows = (await session.execute(select(Booking))).scalars().all()
    midnight = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    actual = {int((b.start_datetime - midnight).total_seconds() // 3600): b.status for b in rows}
    assert actual == {start: after for start, (_, _, after) in spec.items()}


@pytest.mark.parametrize(("end_offset_seconds", "expected"), [(0, "completed"), (1, "confirmed"), (-1, "completed")])
async def test_complete_past_bookings_boundary_is_inclusive(session, test_user, manicure_service, end_offset_seconds, expected):
    """Запись, закончившаяся ровно сейчас, уже выполнена (end <= now); ещё идущая — нет."""
    booking = _booking(test_user.id, manicure_service.id, 11, 12, "confirmed")
    booking.end_datetime = NOW + dt.timedelta(seconds=end_offset_seconds)
    session.add(booking)
    await session.commit()

    await complete_past_bookings(session, MASTER_ID, NOW)

    session.expire_all()
    assert (await session.execute(select(Booking.status))).scalar_one() == expected
