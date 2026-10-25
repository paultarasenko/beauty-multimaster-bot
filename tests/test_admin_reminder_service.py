import asyncio
import datetime as dt

import pytest

from app.database.models import Recurrence
from app.database.repositories.admin_reminder_repository import AdminReminderRepository
from app.database.session import make_session_factory
from app.services.admin_reminder_service import AdminReminderService
from app.services.exceptions import ReminderInPastError
from tests.conftest import MASTER_ID, RIGA_TZ, TEST_DATABASE_URL

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
T = dt.time(19, 30)  # 19:30 по RIGA_TZ (UTC+2) = 17:30 UTC


class Outbox:
    """Подмена отправки в Telegram."""

    def __init__(self, fail: bool = False):
        self.messages: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram is down")
        self.messages.append(text)


def service(session) -> AdminReminderService:
    return AdminReminderService(session, RIGA_TZ, master_id=MASTER_ID)


async def test_create_once_in_past_is_rejected(session):
    with pytest.raises(ReminderInPastError):
        await service(session).create("x", Recurrence.ONCE, dt.date(2026, 10, 1), T, now=NOW)
    assert await AdminReminderRepository(session, MASTER_ID).count() == 0


async def test_create_stores_next_fire_in_utc(session):
    r = await service(session).create("Позвонить", Recurrence.ONCE, dt.date(2026, 10, 5), T, now=NOW)
    await session.commit()
    stored = await AdminReminderRepository(session, MASTER_ID).get(r.id)
    assert stored.next_fire_at == dt.datetime(2026, 10, 5, 17, 30, tzinfo=UTC)


async def test_nothing_sent_before_due_time(session):
    await service(session).create("x", Recurrence.ONCE, dt.date(2026, 10, 5), T, now=NOW)
    await session.commit()
    outbox = Outbox()
    assert await service(session).process_due(outbox, now=NOW) == 0
    assert outbox.messages == []


async def test_once_is_sent_once_and_removed(session):
    await service(session).create("Позвонить", Recurrence.ONCE, dt.date(2026, 10, 5), T, now=NOW)
    await session.commit()
    outbox = Outbox()
    later = dt.datetime(2026, 10, 5, 17, 31, tzinfo=UTC)

    assert await service(session).process_due(outbox, now=later) == 1
    assert await service(session).process_due(outbox, now=later) == 0  # повторный тик — без дубля

    assert outbox.messages == ["Позвонить"]
    assert await AdminReminderRepository(session, MASTER_ID).count() == 0


async def test_recurring_is_rescheduled_and_survives_restart(session):
    reminder_id = (await service(session).create("Налоги", Recurrence.DAILY, dt.date(2026, 10, 5), T, now=NOW)).id
    await session.commit()
    outbox = Outbox()
    fire = dt.datetime(2026, 10, 5, 17, 30, tzinfo=UTC)

    assert await service(session).process_due(outbox, now=fire + dt.timedelta(minutes=1)) == 1

    # «Рестарт»: состояние берётся заново из БД, а не из памяти процесса.
    session.expire_all()
    stored = await AdminReminderRepository(session, MASTER_ID).get(reminder_id)
    assert stored.next_fire_at == dt.datetime(2026, 10, 6, 17, 30, tzinfo=UTC)


async def test_downtime_sends_single_reminder_not_one_per_missed_day(session):
    await service(session).create("Налоги", Recurrence.DAILY, dt.date(2026, 10, 5), T, now=NOW)
    await session.commit()
    outbox = Outbox()
    week_later = dt.datetime(2026, 10, 12, 8, 0, tzinfo=UTC)

    assert await service(session).process_due(outbox, now=week_later) == 1
    assert await service(session).process_due(outbox, now=week_later) == 0
    assert outbox.messages == ["Налоги"]


async def test_failed_send_is_retried_on_next_tick(session):
    await service(session).create("Важное", Recurrence.ONCE, dt.date(2026, 10, 5), T, now=NOW)
    await session.commit()
    later = dt.datetime(2026, 10, 5, 17, 31, tzinfo=UTC)

    assert await service(session).process_due(Outbox(fail=True), now=later) == 0
    assert await AdminReminderRepository(session, MASTER_ID).count() == 1  # откат: напоминание не потеряно

    outbox = Outbox()
    assert await service(session).process_due(outbox, now=later) == 1
    assert outbox.messages == ["Важное"]


async def test_one_failure_does_not_block_other_reminders(session):
    for text in ("первое", "второе"):
        await service(session).create(text, Recurrence.ONCE, dt.date(2026, 10, 5), T, now=NOW)
    await session.commit()

    class FlakyOutbox(Outbox):
        async def __call__(self, text: str) -> None:
            if text == "первое":
                raise RuntimeError("boom")
            await super().__call__(text)

    outbox = FlakyOutbox()
    assert await service(session).process_due(outbox, now=dt.datetime(2026, 10, 5, 17, 31, tzinfo=UTC)) == 1
    assert outbox.messages == ["второе"]


async def test_deleted_reminder_is_not_sent(session):
    reminder_id = (await service(session).create("x", Recurrence.ONCE, dt.date(2026, 10, 5), T, now=NOW)).id
    await session.commit()
    assert await AdminReminderRepository(session, MASTER_ID).delete(reminder_id) is True
    await session.commit()

    outbox = Outbox()
    assert await service(session).process_due(outbox, now=dt.datetime(2026, 10, 5, 17, 31, tzinfo=UTC)) == 0
    assert outbox.messages == []
    assert await AdminReminderRepository(session, MASTER_ID).delete(reminder_id) is False  # повторное удаление безопасно


async def test_list_upcoming_is_sorted_by_next_fire(session):
    for text, day in (("позже", 9), ("раньше", 5), ("между", 7)):
        await service(session).create(text, Recurrence.ONCE, dt.date(2026, 10, day), T, now=NOW)
    await session.commit()

    texts = [r.text for r in await AdminReminderRepository(session, MASTER_ID).list_upcoming(limit=10)]
    assert texts == ["раньше", "между", "позже"]
    assert [r.text for r in await AdminReminderRepository(session, MASTER_ID).list_upcoming(limit=2)] == ["раньше", "между"]


async def test_two_workers_do_not_send_duplicate(session):
    """Две параллельные обработки одного и того же напоминания — отправка ровно одна (настоящая гонка на БД)."""
    await service(session).create("гонка", Recurrence.ONCE, dt.date(2026, 10, 5), T, now=NOW)
    await session.commit()
    later = dt.datetime(2026, 10, 5, 17, 31, tzinfo=UTC)
    session_factory = make_session_factory(TEST_DATABASE_URL)
    outbox = Outbox()

    async def worker() -> int:
        async with session_factory() as s:
            return await AdminReminderService(s, RIGA_TZ, master_id=MASTER_ID).process_due(outbox, now=later)

    results = await asyncio.gather(worker(), worker())

    assert sum(results) == 1
    assert outbox.messages == ["гонка"]
