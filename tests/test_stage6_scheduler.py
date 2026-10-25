"""Единый планировщик всех мастеров (этап 6): реальный PostgreSQL, Telegram замокан (`ScriptedTelegram`)."""

import asyncio
import datetime as dt
import logging

import pytest
import pytest_asyncio
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.methods import SendMessage
from cryptography.fernet import Fernet
from sqlalchemy import select, text, update
from zoneinfo import ZoneInfo

from app.log_context import LogContextFilter
from app.database.models import (
    AdminReminder,
    Booking,
    BookingStatus,
    Master,
    MasterBot,
    MasterClient,
    MasterProfile,
    Notification,
    NotificationStatus,
    Service,
    User,
)
from app.database.session import make_session_factory
from app.multibot.crypto import TokenCipher
from app.multibot.registry import BotRegistry
from app.scheduler.service import SchedulerService
from app.scheduler.tenants import RegistryTenantSource, SingleTenantSource, iter_active_tenants
from app.scheduler.throttle import BotThrottles, RateLimiter
from app.services.reminder_service import ReminderService
from app.utils.recurrence import next_occurrence
from tests.conftest import MASTER_ID, TEST_DATABASE_URL
from tests.test_multibot_registry import add_row
from tests.webhook_utils import FakeTelegram

UTC = dt.timezone.utc
OWNER = lambda m: 1000 + m  # noqa: E731  владелец мастера m
CLIENT = lambda m: 2000 + m  # noqa: E731  клиент мастера m (у каждого свой)
BOT = lambda m: 5000 + m  # noqa: E731  telegram id бота мастера m


class ScriptedTelegram(FakeTelegram):
    """Общая HTTP-сессия всех ботов с управляемыми сбоями по боту (`fail_bot`) и по получателю (`fail_chat`)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_bot: dict[int, object] = {}  # bot_id -> фабрика исключения (method) -> Exception
        self.fail_chat: dict[tuple[int, int], object] = {}  # (bot_id, chat_id) -> фабрика
        self.delay = 0.0
        self.inflight = 0
        self.max_inflight = 0

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                factory = self.fail_bot.get(bot.id) or self.fail_chat.get((bot.id, method.chat_id))
                if factory is not None:
                    self.per_bot.setdefault(bot.id, []).append(method)  # попытка отправки состоялась
                    raise factory(method)
            finally:
                self.inflight -= 1
        return await super().make_request(bot, method, timeout)

    def chats_of(self, bot_id: int) -> list[int]:
        return [m.chat_id for m in self.methods_of(bot_id, SendMessage)]


def unauthorized(method):
    return TelegramUnauthorizedError(method=method, message="Unauthorized")


def retry_after(seconds: int):
    return lambda method: TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=seconds)


def network(method):
    return TelegramNetworkError(method=method, message="connection reset")


def forbidden(method):
    return TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")


def runtime_error(method):
    return RuntimeError("boom")


class World:
    def __init__(self, session, cipher, tg, factory):
        self.session, self.cipher, self.tg, self.factory = session, cipher, tg, factory
        self.registry = BotRegistry(factory, cipher, http_session_factory=lambda: tg)
        self.services: list[SchedulerService] = []

    def scheduler(self, **kwargs) -> SchedulerService:
        service = SchedulerService(self.factory, RegistryTenantSource(self.registry), **kwargs)
        self.services.append(service)
        return service

    async def add_master(self, master_id: int, *, tz: str = "Europe/Riga", bot: bool = True, active: bool = True) -> None:
        """Мастер с владельцем, профилем (часовой пояс), клиентом и (по умолчанию) своим ботом."""
        s = self.session
        if master_id != MASTER_ID:
            s.add(Master(id=master_id, owner_telegram_id=OWNER(master_id), is_active=active))
        else:
            await s.execute(update(Master).where(Master.id == master_id).values(owner_telegram_id=OWNER(master_id), is_active=active))
        s.add(MasterProfile(master_id=master_id, name=f"Мастер {master_id}", timezone=tz))
        user = User(telegram_id=CLIENT(master_id), first_name=f"Клиент {master_id}")
        s.add(user)
        await s.flush()
        s.add(MasterClient(master_id=master_id, user_id=user.id))
        s.add(Service(master_id=master_id, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0))
        await s.commit()
        if bot:
            await add_row(s, self.cipher, master_id, BOT(master_id))

    async def service_id(self, master_id: int) -> int:
        return (await self.session.execute(select(Service.id).where(Service.master_id == master_id))).scalars().first()

    async def client_id(self, master_id: int) -> int:
        return (await self.session.execute(select(User.id).where(User.telegram_id == CLIENT(master_id)))).scalar_one()

    async def add_due_booking(
        self,
        master_id: int,
        *,
        kinds=("reminder_24h",),
        start: dt.datetime | None = None,
        status: str = "confirmed",
        notification_status: str = "pending",
        claimed_at: dt.datetime | None = None,
    ) -> Booking:
        """Подтверждённая запись в будущем + «созревшие» уведомления заданных типов."""
        now = dt.datetime.now(UTC)
        start = start or now + dt.timedelta(days=1) + dt.timedelta(hours=2 * (await self._count_bookings(master_id)))
        booking = Booking(
            master_id=master_id,
            user_id=await self.client_id(master_id),
            service_id=await self.service_id(master_id),
            start_datetime=start,
            end_datetime=start + dt.timedelta(hours=1),
            status=status,
            service_name_snapshot="Маникюр",
            service_price_snapshot=40,
            service_duration_snapshot=60,
        )
        self.session.add(booking)
        await self.session.flush()
        for kind in kinds:
            self.session.add(
                Notification(
                    master_id=master_id,
                    booking_id=booking.id,
                    type=kind,
                    scheduled_at=now - dt.timedelta(minutes=1),
                    status=notification_status,
                    claimed_at=claimed_at,
                )
            )
        await self.session.commit()
        return booking

    async def _count_bookings(self, master_id: int) -> int:
        return len((await self.session.execute(select(Booking.id).where(Booking.master_id == master_id))).all())

    async def statuses(self, master_id: int | None = None) -> list[str]:
        self.session.expire_all()
        query = select(Notification.status).order_by(Notification.id)
        if master_id is not None:
            query = query.where(Notification.master_id == master_id)
        return list((await self.session.execute(query)).scalars().all())

    async def bot_active(self, master_id: int) -> bool:
        self.session.expire_all()
        return (await self.session.execute(select(MasterBot.is_active).where(MasterBot.master_id == master_id))).scalar_one()

    async def close(self) -> None:
        for service in self.services:
            await service.stop(timeout=5)
        await self.registry.close()


@pytest_asyncio.fixture
async def world(session):
    cipher = TokenCipher(Fernet.generate_key().decode())
    w = World(session, cipher, ScriptedTelegram(), make_session_factory(TEST_DATABASE_URL))
    try:
        yield w
    finally:
        await w.close()


async def tick_all(service: SchedulerService) -> None:
    await service.tick_completion()
    await service.tick_notifications()
    await service.tick_admin_reminders()


# ---------------------------------------------------------------- маршрутизация: master_id → его бот


async def test_each_master_reminders_go_through_own_bot_to_own_people(world):
    for m in (1, 2, 3):
        await world.add_master(m)
        await world.add_due_booking(m, kinds=("reminder_24h", "reminder_2h_client", "reminder_2h_master"))

    await world.scheduler().tick_notifications()

    for m in (1, 2, 3):
        # через бот мастера m ушли ровно три сообщения: двум клиентам-напоминания и одно — владельцу мастера m
        assert sorted(world.tg.chats_of(BOT(m))) == sorted([CLIENT(m), CLIENT(m), OWNER(m)])
        assert await world.statuses(m) == ["sent"] * 3
    all_chats = {c for m in (1, 2, 3) for c in world.tg.chats_of(BOT(m))}
    assert all_chats == {CLIENT(m) for m in (1, 2, 3)} | {OWNER(m) for m in (1, 2, 3)}


async def test_admin_reminders_go_only_to_own_owner_via_own_bot(world):
    for m in (1, 2):
        await world.add_master(m)
        world.session.add(
            AdminReminder(
                master_id=m, text=f"напоминание-{m}", recurrence="once", anchor_date=dt.date(2026, 1, 1),
                remind_time=dt.time(9), next_fire_at=dt.datetime.now(UTC) - dt.timedelta(minutes=1),
            )
        )
    await world.session.commit()

    await world.scheduler().tick_admin_reminders()

    assert world.tg.chats_of(BOT(1)) == [OWNER(1)] and world.tg.chats_of(BOT(2)) == [OWNER(2)]
    assert "напоминание-1" in world.tg.texts_of(BOT(1))[0] and "напоминание-2" not in world.tg.texts_of(BOT(1))[0]
    assert "напоминание-2" in world.tg.texts_of(BOT(2))[0]
    assert (await world.session.execute(select(AdminReminder.id))).all() == []  # разовые: отправлены и удалены


async def test_cancelled_booking_notification_is_not_sent_and_marked_cancelled(world):
    await world.add_master(1)
    await world.add_due_booking(1, status="cancelled")
    await world.scheduler().tick_notifications()
    assert world.tg.chats_of(BOT(1)) == [] and await world.statuses() == ["cancelled"]


# ---------------------------------------------------------------- изоляция сбоев


@pytest.mark.parametrize(
    ("factory", "expected_status_a", "bot_a_active", "paused"),
    [
        (unauthorized, "pending", False, False),  # токен недействителен: уведомление не потеряно, бот деактивирован
        (retry_after(40), "pending", True, True),
        (network, "pending", True, True),
    ],
)
async def test_bot_level_failure_of_master_a_does_not_affect_master_b(world, factory, expected_status_a, bot_a_active, paused):
    for m in (1, 2):
        await world.add_master(m)
        await world.add_due_booking(m)
    world.tg.fail_bot[BOT(1)] = factory
    service = world.scheduler()

    await service.tick_notifications()

    assert world.tg.chats_of(BOT(2)) == [CLIENT(2)]  # мастер B получил своё
    assert await world.statuses(2) == ["sent"]
    assert await world.statuses(1) == [expected_status_a]
    assert await world.bot_active(1) is bot_a_active
    assert service.throttles.for_master(1).is_paused is paused


async def test_forbidden_marks_notification_failed_without_retries_and_does_not_stop_the_bot(world):
    """Клиент заблокировал бота: уведомление FAILED, повторов нет, бот мастера продолжает работать."""
    await world.add_master(1)
    await world.add_due_booking(1)
    await world.add_due_booking(1)
    world.tg.fail_chat[(BOT(1), CLIENT(1))] = forbidden
    service = world.scheduler()
    await service.tick_notifications()
    await service.tick_notifications()
    assert await world.statuses(1) == ["failed", "failed"]
    assert len(world.tg.chats_of(BOT(1))) == 2  # по одной попытке на уведомление, без повторов
    assert await world.bot_active(1) is True and not service.throttles.for_master(1).is_paused


async def test_unexpected_error_marks_failed_and_other_masters_still_get_their_messages(world):
    for m in (1, 2):
        await world.add_master(m)
        await world.add_due_booking(m)
    world.tg.fail_bot[BOT(1)] = runtime_error
    await world.scheduler().tick_notifications()
    assert await world.statuses(1) == ["failed"] and await world.statuses(2) == ["sent"]


async def test_exception_inside_one_master_processing_does_not_block_others(world, caplog):
    for m in (1, 2):
        await world.add_master(m, tz="Europe/Riga")
    await world.session.execute(update(MasterProfile).where(MasterProfile.master_id == 1).values(timezone="Mars/Olympus"))
    await world.session.commit()
    end = dt.datetime.now(UTC) - dt.timedelta(hours=1)
    for m in (1, 2):
        world.session.add(
            Booking(master_id=m, user_id=await world.client_id(m), service_id=await world.service_id(m),
                    start_datetime=end - dt.timedelta(hours=1), end_datetime=end, status="confirmed", service_name_snapshot="x", service_price_snapshot=1, service_duration_snapshot=60)
        )
    await world.session.commit()

    with caplog.at_level(logging.ERROR):
        await world.scheduler().tick_completion()

    world.session.expire_all()
    statuses = dict((await world.session.execute(select(Booking.master_id, Booking.status))).all())
    assert statuses == {1: "confirmed", 2: "completed"}  # у мастера 1 сломан часовой пояс — у мастера 2 всё сработало
    assert any("сбой при обработке мастера" in r.getMessage() for r in caplog.records)


async def test_unauthorized_deactivates_bot_logs_clear_error_and_keeps_notification(world, caplog):
    await world.add_master(1)
    await world.add_due_booking(1)
    world.tg.fail_bot[BOT(1)] = unauthorized
    service = world.scheduler()
    caplog.handler.addFilter(LogContextFilter())
    with caplog.at_level(logging.ERROR):
        await service.tick_notifications()

    assert await world.bot_active(1) is False
    assert await world.statuses() == ["pending"]
    record = next(r for r in caplog.records if "недействителен" in r.getMessage())
    assert "помечен неактивным" in record.getMessage()
    assert record.master_id == 1 and record.bot_id == BOT(1)  # контекст лога
    assert not any(":" in r.getMessage() and "AAAA" in r.getMessage() for r in caplog.records)  # токена в логе нет

    # Следующий тик этого мастера не обслуживает; после «переподключения бота оператором» уведомление уходит.
    world.tg.per_bot.clear()
    await service.tick_notifications()
    assert world.tg.chats_of(BOT(1)) == []
    del world.tg.fail_bot[BOT(1)]
    await world.session.execute(update(MasterBot).where(MasterBot.master_id == 1).values(is_active=True))
    await world.session.commit()
    await service.tick_notifications()
    assert world.tg.chats_of(BOT(1)) == [CLIENT(1)] and await world.statuses() == ["sent"]


async def test_retry_after_does_not_become_a_storm_and_loses_nothing(world):
    await world.add_master(1)
    for _ in range(3):
        await world.add_due_booking(1)
    clock = {"t": 1000.0}
    throttles = BotThrottles(25, clock=lambda: clock["t"])
    service = world.scheduler(throttles=throttles)
    world.tg.fail_bot[BOT(1)] = retry_after(30)

    await service.tick_notifications()
    assert len(world.tg.chats_of(BOT(1))) == 1  # одна попытка, остальные не трогали
    assert await world.statuses() == ["pending"] * 3

    await service.tick_notifications()  # пауза ещё идёт: к Telegram вообще не обращаемся
    assert len(world.tg.chats_of(BOT(1))) == 1

    clock["t"] += 31
    del world.tg.fail_bot[BOT(1)]
    await service.tick_notifications()
    assert await world.statuses() == ["sent"] * 3 and len(world.tg.chats_of(BOT(1))) == 4


@pytest.mark.parametrize(("start_in_minutes", "expected"), [(24 * 60, "pending"), (-5, "failed")])
async def test_transient_error_keeps_notification_only_while_booking_has_not_started(world, start_in_minutes, expected):
    """Сеть упала: напоминание ждёт следующего тика, но если запись уже началась — смысла в нём нет (FAILED)."""
    await world.add_master(1)
    await world.add_due_booking(1, start=dt.datetime.now(UTC) + dt.timedelta(minutes=start_in_minutes))
    world.tg.fail_bot[BOT(1)] = network
    await world.scheduler().tick_notifications()
    assert await world.statuses() == [expected]


async def test_notification_survives_transient_error_and_is_delivered_on_a_later_tick(world):
    await world.add_master(1)
    await world.add_due_booking(1)
    clock = {"t": 0.0}
    service = world.scheduler(throttles=BotThrottles(25, clock=lambda: clock["t"]))
    world.tg.fail_bot[BOT(1)] = network
    await service.tick_notifications()
    assert await world.statuses() == ["pending"]
    del world.tg.fail_bot[BOT(1)]
    clock["t"] += 31  # пауза бота после сетевой ошибки истекла
    await service.tick_notifications()
    assert await world.statuses() == ["sent"] and world.tg.chats_of(BOT(1)).count(CLIENT(1)) == 2


# ---------------------------------------------------------------- дубли и параллельные тики


async def test_two_parallel_ticks_do_not_duplicate(world):
    await world.add_master(1)
    for _ in range(6):
        await world.add_due_booking(1)
    world.tg.delay = 0.02
    service = world.scheduler()

    await asyncio.gather(service.tick_notifications(), service.tick_notifications(), service.tick_notifications())

    assert len(world.tg.chats_of(BOT(1))) == 6 and await world.statuses() == ["sent"] * 6


async def test_two_processes_do_not_duplicate(world):
    """Два независимых «процесса»: свои пулы соединений и свои реестры, одна БД."""
    await world.add_master(1)
    await world.add_master(2)
    for m in (1, 2):
        for _ in range(4):
            await world.add_due_booking(m)
    world.tg.delay = 0.02
    other_factory = make_session_factory(TEST_DATABASE_URL)
    other_registry = BotRegistry(other_factory, world.cipher, http_session_factory=lambda: world.tg)
    second = SchedulerService(other_factory, RegistryTenantSource(other_registry))
    try:
        await asyncio.gather(world.scheduler().tick_notifications(), second.tick_notifications())
    finally:
        await other_registry.close()
    assert len(world.tg.chats_of(BOT(1))) == 4 and len(world.tg.chats_of(BOT(2))) == 4


async def test_admin_reminder_not_duplicated_by_parallel_ticks(world):
    await world.add_master(1)
    world.session.add(
        AdminReminder(master_id=1, text="x", recurrence="daily", anchor_date=dt.date(2026, 1, 1), remind_time=dt.time(9),
                      next_fire_at=dt.datetime.now(UTC) - dt.timedelta(minutes=1))
    )
    await world.session.commit()
    world.tg.delay = 0.02
    service = world.scheduler()
    await asyncio.gather(service.tick_admin_reminders(), service.tick_admin_reminders())
    assert len(world.tg.chats_of(BOT(1))) == 1


async def test_stale_sending_is_returned_to_queue_but_fresh_sending_is_not_touched(world):
    await world.add_master(1)
    now = dt.datetime.now(UTC)
    await world.add_due_booking(1, notification_status="sending", claimed_at=now - dt.timedelta(minutes=30))  # завис
    await world.add_due_booking(1, notification_status="sending", claimed_at=now - dt.timedelta(minutes=1))  # в работе
    await world.scheduler().tick_notifications()
    assert await world.statuses() == ["sent", "sending"]
    assert len(world.tg.chats_of(BOT(1))) == 1


async def test_batch_size_limits_one_tick(world):
    await world.add_master(1)
    for _ in range(5):
        await world.add_due_booking(1)
    sent: list[int] = []

    async def send(booking):
        sent.append(booking.id)

    async with world.factory() as s:
        service = ReminderService(s, send, master_id=1, batch_size=2)
        assert await service.process_due_notifications() == 2
        assert await service.process_due_notifications() == 2
        assert await service.process_due_notifications() == 1
    assert await world.statuses() == ["sent"] * 5


async def test_sending_happens_outside_a_database_transaction(world):
    """Во время отправки в Telegram у приложения нет открытых транзакций, держащих строки уведомлений."""
    await world.add_master(1)
    await world.add_due_booking(1)
    seen: list[int] = []

    async def probe(booking):
        async with world.factory() as s:
            # строка уведомления не заблокирована: FOR UPDATE NOWAIT проходит, пока «идёт отправка»
            row = (await s.execute(select(Notification.id).with_for_update(nowait=True))).scalars().all()
            seen.extend(row)

    async with world.factory() as s:
        assert await ReminderService(s, probe, master_id=1).process_due_notifications() == 1
    assert len(seen) == 1


# ---------------------------------------------------------------- состав обслуживаемых мастеров


async def test_bot_added_and_disabled_between_ticks_without_restart(world):
    await world.add_master(1)
    await world.add_master(2, bot=False)
    await world.add_due_booking(1)
    await world.add_due_booking(2)
    service = world.scheduler()

    await service.tick_notifications()
    assert world.tg.chats_of(BOT(1)) == [CLIENT(1)] and world.tg.chats_of(BOT(2)) == []  # у мастера 2 бота ещё нет
    assert await world.statuses(2) == ["pending"]

    await add_row(world.session, world.cipher, 2, BOT(2))  # «CLI add-bot» при работающем сервисе
    await world.add_due_booking(1)
    await service.tick_notifications()
    assert world.tg.chats_of(BOT(2)) == [CLIENT(2)] and await world.statuses(2) == ["sent"]

    await world.session.execute(update(MasterBot).where(MasterBot.master_id == 1).values(is_active=False))  # «remove-bot / отключили»
    await world.session.commit()
    before = len(world.tg.chats_of(BOT(1)))
    await world.add_due_booking(1)
    await service.tick_notifications()
    assert len(world.tg.chats_of(BOT(1))) == before and (await world.statuses(1))[-1] == "pending"


async def test_inactive_master_and_inactive_bot_are_skipped(world):
    await world.add_master(1, active=False)
    await world.add_master(2)
    await world.add_master(3)
    await world.session.execute(update(MasterBot).where(MasterBot.master_id == 3).values(is_active=False))
    await world.session.commit()
    for m in (1, 2, 3):
        await world.add_due_booking(m)
    service = world.scheduler()
    await tick_all(service)
    assert world.tg.chats_of(BOT(1)) == [] and world.tg.chats_of(BOT(3)) == [] and world.tg.chats_of(BOT(2)) == [CLIENT(2)]
    assert [t.master_id for t in await iter_active_tenants(RegistryTenantSource(world.registry))] == [2]


async def test_polling_source_serves_only_the_configured_active_master(world):
    from aiogram import Bot

    await world.add_master(1, bot=False)
    bot = Bot(token=f"{BOT(1)}:{'A' * 35}", session=world.tg)
    source = SingleTenantSource(world.factory, bot, 1)
    assert [t.master_id for t in await iter_active_tenants(source)] == [1]
    await world.session.execute(update(Master).where(Master.id == 1).values(is_active=False))
    await world.session.commit()
    assert await iter_active_tenants(source) == []


async def test_polling_mode_scheduler_delivers_like_before(world):
    from aiogram import Bot

    await world.add_master(1, bot=False)
    await world.add_due_booking(1)
    bot = Bot(token=f"{BOT(1)}:{'A' * 35}", session=world.tg)
    service = SchedulerService(world.factory, SingleTenantSource(world.factory, bot, 1))
    scheduler = service.start()
    try:
        assert {j.id for j in scheduler.get_jobs()} == {"reminder_worker", "admin_reminder_worker", "booking_completion_worker"}
        await scheduler.get_job("reminder_worker").func()
    finally:
        await service.stop()
    assert world.tg.chats_of(BOT(1)) == [CLIENT(1)]


async def test_apscheduler_jobs_are_configured_against_overlaps(world):
    service = world.scheduler()
    scheduler = service.start()
    for job in scheduler.get_jobs():
        assert job.max_instances == 1 and job.coalesce is True and job.misfire_grace_time
    assert service._scheduler is scheduler


# ---------------------------------------------------------------- часовые пояса мастеров


async def test_admin_reminder_next_fire_is_computed_in_each_masters_timezone(world):
    zones = {1: "Asia/Tokyo", 2: "Pacific/Pago_Pago", 3: "Europe/Riga"}
    for m, tz in zones.items():
        await world.add_master(m, tz=tz)
        world.session.add(
            AdminReminder(master_id=m, text="x", recurrence="daily", anchor_date=dt.date(2026, 1, 5), remind_time=dt.time(9),
                          next_fire_at=dt.datetime.now(UTC) - dt.timedelta(minutes=1))
        )
    await world.session.commit()

    await world.scheduler().tick_admin_reminders()

    world.session.expire_all()
    rows = (await world.session.execute(select(AdminReminder.master_id, AdminReminder.next_fire_at))).all()
    assert len(rows) == 3
    for master_id, next_fire in rows:
        local = next_fire.astimezone(ZoneInfo(zones[master_id]))
        assert local.time() == dt.time(9, 0), (master_id, local)  # настенные 09:00 — по часам именно ЭТОГО мастера
        assert next_fire > dt.datetime.now(UTC)
    assert len({next_fire.hour for _, next_fire in rows}) == 3  # в UTC это три разных часа


@pytest.mark.parametrize("tz", ["Pacific/Kiritimati", "Pacific/Pago_Pago", "Europe/Riga", "America/Los_Angeles"])
async def test_completion_uses_masters_clock(world, tz):
    await world.add_master(1, tz=tz)
    now = dt.datetime.now(UTC)
    uid, sid = await world.client_id(1), await world.service_id(1)
    for name, delta in (("done", -30), ("running", +30)):  # закончилась 30 минут назад / закончится через 30 минут
        end = now + dt.timedelta(minutes=delta)
        world.session.add(
            Booking(master_id=1, user_id=uid, service_id=sid, start_datetime=end - dt.timedelta(hours=1), end_datetime=end, status="confirmed",
                    service_name_snapshot=name, service_price_snapshot=1, service_duration_snapshot=60)
        )
    await world.session.commit()
    await world.scheduler().tick_completion()
    world.session.expire_all()
    got = dict((await world.session.execute(select(Booking.service_name_snapshot, Booking.status))).all())
    assert got == {"done": BookingStatus.COMPLETED.value, "running": BookingStatus.CONFIRMED.value}


# ---------------------------------------------------------------- ограничение скорости на бота


async def test_rate_limiter_spaces_sends_and_is_independent_per_bot():
    clock = {"t": 0.0}
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    throttles = BotThrottles(10, clock=lambda: clock["t"], sleep=fake_sleep)
    a, b = throttles.for_master(1).limiter, throttles.for_master(2).limiter
    for _ in range(31):
        await a.acquire()
    assert 2.9 < sum(slept) < 3.1  # 31 сообщение при 10/с — около трёх секунд, равномерно
    assert max(slept) <= 0.1001
    slept.clear()
    await b.acquire()  # бот B не ждёт из-за загруженности бота A
    assert slept == []
    assert throttles.for_master(1) is throttles.for_master(1) and throttles.for_master(1) is not throttles.for_master(2)
    throttles.forget_except({2})
    assert throttles.for_master(1) is not a  # забытый бот получает новое состояние


async def test_rate_limiter_rejects_bad_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)


async def test_scheduler_sends_through_per_bot_rate_limit(world):
    await world.add_master(1)
    await world.add_master(2)
    for m in (1, 2):
        for _ in range(5):
            await world.add_due_booking(m)
    clock = {"t": 0.0}
    sleeps: list[tuple[str, float]] = []

    async def fake_sleep(seconds):
        sleeps.append(("s", seconds))
        clock["t"] += seconds

    service = world.scheduler(throttles=BotThrottles(2, clock=lambda: clock["t"], sleep=fake_sleep))
    await service.tick_notifications()
    assert len(world.tg.chats_of(BOT(1))) == 5 and len(world.tg.chats_of(BOT(2))) == 5
    assert sum(s for _, s in sleeps) > 0  # при 2 сообщениях в секунду пятерка не уходит мгновенно


# ---------------------------------------------------------------- 100 мастеров


async def test_smoke_hundred_masters_respect_bounded_concurrency(world):
    import time

    count = 100
    world.session.add_all(Master(id=m, owner_telegram_id=OWNER(m), is_active=True) for m in range(2, count + 1))
    await world.session.execute(update(Master).where(Master.id == 1).values(owner_telegram_id=OWNER(1)))
    await world.session.commit()
    users = [User(telegram_id=CLIENT(m), first_name=f"c{m}") for m in range(1, count + 1)]
    world.session.add_all(users)
    await world.session.flush()
    world.session.add_all(MasterClient(master_id=m, user_id=users[m - 1].id) for m in range(1, count + 1))
    services = [Service(master_id=m, name="Маникюр", price=1, duration_minutes=60, active=True, sort_order=0) for m in range(1, count + 1)]
    world.session.add_all(services)
    await world.session.commit()
    for m in range(1, count + 1):
        await add_row(world.session, world.cipher, m, BOT(m))
    now = dt.datetime.now(UTC)
    bookings = [
        Booking(master_id=m, user_id=users[m - 1].id, service_id=services[m - 1].id, start_datetime=now + dt.timedelta(days=1),
                end_datetime=now + dt.timedelta(days=1, hours=1), status="confirmed", service_name_snapshot="x",
                service_price_snapshot=1, service_duration_snapshot=60)
        for m in range(1, count + 1)
    ]
    world.session.add_all(bookings)
    await world.session.flush()
    world.session.add_all(
        Notification(master_id=m, booking_id=bookings[m - 1].id, type="reminder_24h", scheduled_at=now - dt.timedelta(minutes=1), status="pending")
        for m in range(1, count + 1)
    )
    await world.session.commit()
    world.tg.delay = 0.01
    service = world.scheduler(max_concurrent=5)

    started = time.monotonic()
    await tick_all(service)
    elapsed = time.monotonic() - started

    assert all(world.tg.chats_of(BOT(m)) == [CLIENT(m)] for m in range(1, count + 1))  # каждому — через свой бот
    assert set(await world.statuses()) == {"sent"}
    assert world.tg.max_inflight <= 5
    assert elapsed < 30, elapsed


# ---------------------------------------------------------------- жизненный цикл


async def test_webhook_app_lifespan_starts_and_stops_scheduler(world):
    from app.bot.dispatcher import build_dispatcher, default_routers
    from app.multibot.webhook import create_webhook_app

    service = world.scheduler()
    dp = build_dispatcher(world.factory, workflow_data={"bot_registry": world.registry})
    app = create_webhook_app(world.registry, dp, max_body_bytes=1000, scheduler=service)
    try:
        assert service._scheduler is None
        async with app.router.lifespan_context(app):
            assert service._scheduler is not None and len(service._scheduler.get_jobs()) == 3
        assert service._scheduler is None
    finally:
        for router in default_routers():
            router._parent_router = None  # модульные роутеры подключаются к диспетчеру один раз за тест


async def test_stop_waits_for_running_tick_to_finish_sending(world):
    await world.add_master(1)
    for _ in range(3):
        await world.add_due_booking(1)
    world.tg.delay = 0.1
    service = world.scheduler()
    tick = asyncio.create_task(service.tick_notifications())
    await asyncio.sleep(0.05)  # тик уже в процессе отправки
    await service.stop(timeout=10)
    assert tick.done() and await world.statuses() == ["sent"] * 3


# ---------------------------------------------------------------- админские напоминания: ошибки отправки


async def _due_admin_reminder(world, master_id: int, recurrence: str = "daily") -> None:
    world.session.add(
        AdminReminder(master_id=master_id, text="x", recurrence=recurrence, anchor_date=dt.date(2026, 1, 5), remind_time=dt.time(9),
                      next_fire_at=dt.datetime.now(UTC) - dt.timedelta(minutes=1))
    )
    await world.session.commit()


async def test_admin_reminder_to_owner_who_blocked_bot_is_skipped_not_retried_every_tick(world):
    await world.add_master(1)
    await _due_admin_reminder(world, 1, "daily")
    world.tg.fail_bot[BOT(1)] = forbidden
    service = world.scheduler()
    await service.tick_admin_reminders()
    await service.tick_admin_reminders()
    assert len(world.tg.chats_of(BOT(1))) == 1  # одна попытка: срабатывание пропущено, следующее — завтра
    world.session.expire_all()
    assert (await world.session.execute(select(AdminReminder.next_fire_at))).scalar_one() > dt.datetime.now(UTC)


@pytest.mark.parametrize("factory", [network, retry_after(20)])
async def test_admin_reminder_survives_temporary_failure_and_other_masters_are_served(world, factory):
    for m in (1, 2):
        await world.add_master(m)
        await _due_admin_reminder(world, m, "once")
    world.tg.fail_bot[BOT(1)] = factory
    service = world.scheduler()
    await service.tick_admin_reminders()
    world.session.expire_all()
    left = (await world.session.execute(select(AdminReminder.master_id))).scalars().all()
    assert left == [1]  # у мастера 1 осталось (повторится), у мастера 2 отправлено и удалено
    assert world.tg.chats_of(BOT(2)) == [OWNER(2)] and service.throttles.for_master(1).is_paused


async def test_admin_reminder_unauthorized_deactivates_bot_and_keeps_reminder(world):
    await world.add_master(1)
    await _due_admin_reminder(world, 1, "once")
    world.tg.fail_bot[BOT(1)] = unauthorized
    await world.scheduler().tick_admin_reminders()
    assert await world.bot_active(1) is False
    assert (await world.session.execute(select(AdminReminder.id))).all() != []


# ---------------------------------------------------------------- мгновенные уведомления мастеру (пункт 7)


async def test_client_blocking_bot_notifies_owner_of_that_master_through_that_masters_bot(world, session):
    """Блокировка бота клиентом, пришедшая в бот мастера B: уведомление владельцу B, через бот B; бот A молчит."""
    import httpx

    from app.bot.dispatcher import build_dispatcher, default_routers
    from app.multibot.webhook import create_webhook_app

    await world.add_master(1)
    await world.add_master(2)
    await world.registry.load_all()
    dp = build_dispatcher(world.factory, workflow_data={"bot_registry": world.registry})
    app = create_webhook_app(world.registry, dp, max_body_bytes=100_000)
    row = (await session.execute(select(MasterBot).where(MasterBot.master_id == 2))).scalar_one()
    payload = {
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": 777, "type": "private"},
            "from": {"id": 777, "is_bot": False, "first_name": "Анна", "username": "anna"},
            "date": int(dt.datetime.now(UTC).timestamp()),
            "old_chat_member": {"status": "member", "user": {"id": BOT(2), "is_bot": True, "first_name": "b"}},
            "new_chat_member": {"status": "kicked", "until_date": 0, "user": {"id": BOT(2), "is_bot": True, "first_name": "b"}},
        },
    }
    from app.multibot.webhook import SECRET_HEADER

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/tg/{BOT(2)}/{row.webhook_secret}", json=payload,
                headers={SECRET_HEADER: world.cipher.header_token(row.webhook_secret)},
            )
        assert response.status_code == 200
    finally:
        for router in default_routers():
            router._parent_router = None
    assert world.tg.chats_of(BOT(2)) == [OWNER(2)] and "заблокировал бота" in world.tg.texts_of(BOT(2))[0]
    assert world.tg.chats_of(BOT(1)) == []


def test_every_master_notifier_is_built_from_the_receiving_bot_and_the_tenants_owner():
    """Статический сторож: MasterNotifier создаётся только из бота, принявшего апдейт, и владельца ТЕКУЩЕГО мастера."""
    import ast
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    allowed_bots = {"callback.bot", "message.bot", "event.bot"}
    found = 0
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "MasterNotifier":
                found += 1
                kwargs = {k.arg: ast.unparse(k.value) for k in node.keywords}
                assert ast.unparse(node.args[0]) in allowed_bots, f"{path}:{node.lineno}: чужой/общий бот"
                assert kwargs.get("master_id") == "tenant.master_id", f"{path}:{node.lineno}"
                assert kwargs.get("owner_telegram_id") == "tenant.master.owner_telegram_id", f"{path}:{node.lineno}"
    assert found >= 8
    # Ни одно место отправки не берёт «общий» токен или id администратора из настроек.
    for path in app_dir.rglob("*.py"):
        if path.name in {"settings.py", "owner.py", "main.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"settings\.(bot_token|admin_telegram_id)\b", source), path
