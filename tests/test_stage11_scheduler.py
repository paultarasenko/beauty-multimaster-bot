"""Этап 11, планировщик: уведомления мастера уходят только его ботом и только его людям; сбой одного мастера не мешает
другому; два конкурентных воркера не дублируют отправку; отключённые мастер/бот пропускаются, а после включения устаревшие
напоминания не уходят пачкой (поведение этапа 10).
"""

import asyncio
import datetime as dt

import pytest_asyncio
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.database.models import AdminReminder, Booking, BookingStatus, MasterBot, Notification, NotificationStatus
from app.database.session import make_session_factory
from app.multibot.registry import BotRegistry
from app.scheduler.service import SchedulerService
from app.scheduler.tenants import RegistryTenantSource
from app.superadmin import service as superadmin_service
from tests.conftest import TEST_DATABASE_URL
from tests.stage11_support import A, B, BOT_ID, ONLY_TG, OWNER, SHARED_TG, start_at, two_masters  # noqa: F401  (фикстура pytest)
from tests.test_stage6_scheduler import ScriptedTelegram, forbidden, unauthorized

PENDING, SENT, CANCELLED = (s.value for s in (NotificationStatus.PENDING, NotificationStatus.SENT, NotificationStatus.CANCELLED))


class Env:
    def __init__(self, session, ns, tg, factory, registry, scheduler):
        self.session, self.ns, self.tg, self.factory, self.registry, self.scheduler = session, ns, tg, factory, registry, scheduler
        self.extra: list = []

    async def add_due(self, master: int, hour: int, user_id: int, minutes_ago: int = 5) -> int:
        """Подтверждённая запись (в будущем) и уже «созревшее» напоминание за 24 часа."""
        ns = self.ns
        booking = Booking(
            master_id=master, user_id=user_id, service_id=ns.service_id[master],
            start_datetime=start_at(ns.day, hour), end_datetime=start_at(ns.day, hour + 1),
            status=BookingStatus.CONFIRMED.value, service_name_snapshot="s", service_price_snapshot=1, service_duration_snapshot=60,
        )
        self.session.add(booking)
        await self.session.flush()
        notification = Notification(
            master_id=master, booking_id=booking.id, type="reminder_24h",
            scheduled_at=ns.now - dt.timedelta(minutes=minutes_ago), status=PENDING,
        )
        self.session.add(notification)
        await self.session.commit()
        return notification.id

    async def statuses(self, master: int) -> list[str]:
        self.session.expire_all()
        rows = await self.session.execute(select(Notification.status).where(Notification.master_id == master).order_by(Notification.id))
        return list(rows.scalars().all())

    async def bot_active(self, master: int) -> bool:
        self.session.expire_all()
        return (await self.session.execute(select(MasterBot.is_active).where(MasterBot.master_id == master))).scalar_one()

    def chats(self, master: int) -> list[int]:
        return self.tg.chats_of(BOT_ID[master])


@pytest_asyncio.fixture
async def env(session, two_masters):
    tg = ScriptedTelegram()
    factory = make_session_factory(TEST_DATABASE_URL)
    registry = BotRegistry(factory, two_masters.cipher, http_session_factory=lambda: tg)
    scheduler = SchedulerService(factory, RegistryTenantSource(registry))
    e = Env(session, two_masters, tg, factory, registry, scheduler)
    try:
        yield e
    finally:
        await scheduler.stop(timeout=5)
        await registry.close()
        for extra in e.extra:
            await extra()
        await factory.kw["bind"].dispose()


# ---------------------------------------------------------------- маршрутизация


async def test_notifications_go_through_own_bot_to_own_clients_only(env):
    await env.add_due(A, 8, env.ns.only_id[A])
    await env.add_due(B, 8, env.ns.only_id[B])
    await env.scheduler.tick_notifications()

    assert sorted(env.chats(A)) == sorted([SHARED_TG, ONLY_TG[A]])
    assert sorted(env.chats(B)) == sorted([SHARED_TG, ONLY_TG[B]])
    assert ONLY_TG[B] not in env.chats(A) and ONLY_TG[A] not in env.chats(B)
    assert OWNER[A] not in env.chats(B) and OWNER[B] not in env.chats(A)
    assert await env.statuses(A) == [SENT, PENDING, SENT] and await env.statuses(B) == [SENT, PENDING, SENT]


async def test_personal_reminders_of_master_go_only_to_its_owner_via_its_bot(env):
    await env.scheduler.tick_admin_reminders()
    assert env.chats(A) == [OWNER[A]] and env.chats(B) == [OWNER[B]]
    sent_a = [m.text for m in env.tg.methods_of(BOT_ID[A], SendMessage)]
    assert any("напоминание A" in t for t in sent_a) and not any("напоминание B" in t for t in sent_a)
    env.session.expire_all()
    rows = (await env.session.execute(select(AdminReminder.master_id, AdminReminder.next_fire_at).order_by(AdminReminder.master_id))).all()
    # Срабатывание сдвинуто у обоих мастеров (дата «один раз» в будущем — напоминание переносится на неё, а не удаляется).
    assert [m for m, _ in rows] == [A, B] and all(fire > env.ns.now for _, fire in rows)


# ---------------------------------------------------------------- сбои


async def test_invalid_token_of_a_does_not_stop_b(env):
    env.tg.fail_bot[BOT_ID[A]] = unauthorized
    await env.scheduler.tick_notifications()
    assert await env.statuses(A) == [PENDING, PENDING]  # у A ничего не потеряно
    assert await env.bot_active(A) is False and await env.bot_active(B) is True
    assert env.chats(B) == [SHARED_TG] and await env.statuses(B) == [SENT, PENDING]


async def test_client_of_a_blocking_the_bot_does_not_affect_b(env):
    await env.add_due(A, 8, env.ns.only_id[A])
    await env.add_due(B, 8, env.ns.only_id[B])
    env.tg.fail_chat[(BOT_ID[A], ONLY_TG[A])] = forbidden
    await env.scheduler.tick_notifications()
    assert await env.statuses(B) == [SENT, PENDING, SENT]
    assert await env.statuses(A) == [SENT, PENDING, NotificationStatus.FAILED.value]
    assert await env.bot_active(A) is True  # блокировка клиентом — не отказ бота


# ---------------------------------------------------------------- конкурентные воркеры


async def test_two_concurrent_workers_send_every_notification_once(env):
    for hour in (8, 9, 10, 11):
        await env.add_due(A, hour, env.ns.only_id[A])
        await env.add_due(B, hour, env.ns.only_id[B])
    env.tg.delay = 0.02
    factory2 = make_session_factory(TEST_DATABASE_URL)
    registry2 = BotRegistry(factory2, env.ns.cipher, http_session_factory=lambda: env.tg)
    second = SchedulerService(factory2, RegistryTenantSource(registry2))

    async def close():
        await registry2.close()
        await factory2.kw["bind"].dispose()

    env.extra.append(close)
    await asyncio.gather(env.scheduler.tick_notifications(), second.tick_notifications(), env.scheduler.tick_notifications())

    for master in (A, B):
        assert len(env.chats(master)) == 5, "каждое созревшее уведомление должно уйти ровно один раз"
        assert await env.statuses(master) == [SENT, PENDING] + [SENT] * 4
    assert sorted(env.chats(A)).count(ONLY_TG[A]) == 4 and ONLY_TG[B] not in env.chats(A)


# ---------------------------------------------------------------- отключение и повторное включение


async def test_disabled_master_and_bot_are_skipped_and_stale_reminders_do_not_flood_after_enable(env):
    await superadmin_service.set_master_active(env.factory, B, False)  # мастер B отключён
    await superadmin_service.set_bot_active(env.factory, A, False)  # бот A отключён
    await env.scheduler.tick_notifications()
    await env.scheduler.tick_admin_reminders()
    assert env.tg.per_bot == {}  # никто не обслуживался
    assert await env.statuses(A) == [PENDING, PENDING] and await env.statuses(B) == [PENDING, PENDING]

    await superadmin_service.set_master_active(env.factory, B, True)  # включили B — устаревшее отменено, только у B
    assert await env.statuses(B) == [CANCELLED, PENDING]  # просроченное — отменено, будущее (через 10 дней) осталось
    assert await env.statuses(A) == [PENDING, PENDING]  # у соседа — ничего не тронуто

    await env.scheduler.tick_notifications()
    assert env.chats(B) == [] and env.chats(A) == []  # пачки устаревших сообщений нет; бот A всё ещё выключен
    assert await env.statuses(B) == [CANCELLED, PENDING]

    await superadmin_service.set_bot_active(env.factory, A, True)
    assert await env.statuses(A) == [CANCELLED, PENDING]
    await env.scheduler.tick_notifications()
    assert env.chats(A) == [] and env.chats(B) == []
    assert await env.statuses(A) == [CANCELLED, PENDING] and await env.statuses(B) == [CANCELLED, PENDING]
