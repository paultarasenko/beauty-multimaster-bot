import datetime as dt

import pytest

from app.database.models import WorkingHours
from app.services.booking_service import BookingService
from app.services.reminder_service import ReminderService
from tests.conftest import MASTER_ID, next_weekday

RIGA_TZ = dt.timezone(dt.timedelta(hours=2))
NEXT_FRIDAY = next_weekday(4)  # пятница через 2+ недели: дата всегда в будущем


async def _make_confirmed_booking(session, test_user, manicure_service, hour=15):
    session.add(
        WorkingHours(master_id=MASTER_ID,
            weekday=NEXT_FRIDAY.weekday(), is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0)
        )
    )
    await session.commit()
    svc = BookingService(session, RIGA_TZ, reminder_hours_before=24, master_id=MASTER_ID)
    start = dt.datetime.combine(NEXT_FRIDAY, dt.time(hour, 0), tzinfo=RIGA_TZ)
    booking = await svc.create_booking(test_user.id, manicure_service.id, start)
    await session.commit()
    return booking


@pytest.mark.asyncio
async def test_reminder_created_on_booking(session, test_user, manicure_service):
    booking = await _make_confirmed_booking(session, test_user, manicure_service)
    expected_reminder_time = booking.start_datetime - dt.timedelta(hours=24)

    reminder_svc = ReminderService(session, send_message_func=lambda b: None, master_id=MASTER_ID)
    due = await reminder_svc.get_due_notifications(now=expected_reminder_time - dt.timedelta(minutes=1))
    assert due == []  # ещё рано
    due = await reminder_svc.get_due_notifications(now=expected_reminder_time)
    assert len(due) == 1
    assert due[0].booking_id == booking.id


@pytest.mark.asyncio
async def test_reminder_sent_exactly_once(session, test_user, manicure_service):
    booking = await _make_confirmed_booking(session, test_user, manicure_service)
    send_calls = []

    async def fake_send(b):
        send_calls.append(b.id)

    reminder_svc = ReminderService(session, send_message_func=fake_send, master_id=MASTER_ID)
    now = booking.start_datetime - dt.timedelta(hours=24)

    sent_count = await reminder_svc.process_due_notifications(now=now)
    assert sent_count == 1
    assert send_calls == [booking.id]

    # Повторный запуск воркера (например, после рестарта приложения) — уже sent, не шлём снова.
    sent_count_2 = await reminder_svc.process_due_notifications(now=now + dt.timedelta(minutes=1))
    assert sent_count_2 == 0
    assert send_calls == [booking.id]  # всё ещё один вызов


@pytest.mark.asyncio
async def test_reminder_not_sent_for_cancelled_booking(session, test_user, manicure_service):
    from app.services.booking_service import BookingService

    booking = await _make_confirmed_booking(session, test_user, manicure_service)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    await svc.cancel_booking(booking.id, now=dt.datetime(2026, 10, 1, tzinfo=RIGA_TZ))
    await session.commit()

    send_calls = []

    async def fake_send(b):
        send_calls.append(b.id)

    reminder_svc = ReminderService(session, send_message_func=fake_send, master_id=MASTER_ID)
    now = booking.start_datetime - dt.timedelta(hours=24)
    sent_count = await reminder_svc.process_due_notifications(now=now)

    assert sent_count == 0
    assert send_calls == []


@pytest.mark.asyncio
async def test_reminder_survives_restart_simulation(session, test_user, manicure_service):
    """Симулируем рестарт: создаём новый ReminderService (как будто новый процесс поднялся)
    и проверяем, что due-уведомление из БД по-прежнему находится и отправляется."""
    booking = await _make_confirmed_booking(session, test_user, manicure_service)
    now = booking.start_datetime - dt.timedelta(hours=24)

    # "Первый процесс" ничего не успел сделать, просто закончился.
    del booking

    # "Второй процесс после рестарта" — новый ReminderService на той же сессии/БД.
    send_calls = []

    async def fake_send(b):
        send_calls.append(b.id)

    reminder_svc = ReminderService(session, send_message_func=fake_send, master_id=MASTER_ID)
    sent_count = await reminder_svc.process_due_notifications(now=now)
    assert sent_count == 1
    assert len(send_calls) == 1


@pytest.mark.asyncio
async def test_two_hour_reminders_created_for_client_and_master(session, test_user, manicure_service):
    from sqlalchemy import select

    from app.database.models import Notification

    booking = await _make_confirmed_booking(session, test_user, manicure_service)
    result = await session.execute(select(Notification).where(Notification.booking_id == booking.id))
    by_type = {n.type: n for n in result.scalars().all()}

    assert set(by_type) == {"reminder_24h", "reminder_2h_client", "reminder_2h_master"}
    expected = booking.start_datetime - dt.timedelta(hours=2)
    assert by_type["reminder_2h_client"].scheduled_at == expected
    assert by_type["reminder_2h_master"].scheduled_at == expected


@pytest.mark.asyncio
async def test_two_hour_reminders_routed_by_type(session, test_user, manicure_service):
    booking = await _make_confirmed_booking(session, test_user, manicure_service)
    calls = []

    async def send_default(b):
        calls.append(("default", b.id))

    async def send_client_2h(b):
        calls.append(("client_2h", b.id))

    async def send_master_2h(b):
        calls.append(("master_2h", b.id))

    reminder_svc = ReminderService(
        session,
        send_message_func=send_default,
        send_funcs_by_type={"reminder_2h_client": send_client_2h, "reminder_2h_master": send_master_2h},
        master_id=MASTER_ID,
    )
    now = booking.start_datetime - dt.timedelta(hours=2)
    sent_count = await reminder_svc.process_due_notifications(now=now)

    # 24h-напоминание тоже уже «просрочено» на этот момент и уходит через функцию по умолчанию.
    assert sent_count == 3
    assert sorted(calls) == sorted(
        [("default", booking.id), ("client_2h", booking.id), ("master_2h", booking.id)]
    )
