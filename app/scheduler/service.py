"""Единый планировщик всех мастеров (этап 6).

Цепочка отправки: уведомление/напоминание → master_id → бот этого мастера (`ServedTenant.bot`, из BotRegistry) → Telegram.
Один процесс — ОДИН набор из трёх задач на всех мастеров (прежние id сохранены); при включённом суперадмин-боте к ним добавляется
четвёртая, служебная — `trial_digest_worker` (ежедневная сводка суперадмину о сроках пробного периода, этап 10; мастеров не обходит):

- `reminder_worker` — напоминания по записям (клиенту за 24 и 2 часа, мастеру за 2 часа) из notifications;
- `admin_reminder_worker` — личные напоминания мастера;
- `booking_completion_worker` — перевод прошедших записей в «выполнена».

На каждом тике задача спрашивает у шлюза `iter_active_tenants`, кого обслуживать, и обрабатывает мастеров параллельно, но не
более `scheduler_max_concurrent_tenants` одновременно (общий семафор на все задачи — он же ограничивает число соединений с БД).
Мастер обрабатывается существующей per-master логикой (ReminderService, AdminReminderService, complete_past_bookings,
часовой пояс мастера); сбой одного мастера никогда не мешает остальным (см. `_run_tenant`).
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.settings import settings
from app.database.models import NotificationType
from app.log_context import update_log_context
from app.scheduler.locks import tick_lock
from app.scheduler.reminder_worker import (
    make_send_admin_reminder_func,
    make_send_deferred_func,
    make_send_client_reminder_2h_func,
    make_send_master_reminder_2h_func,
    make_send_reminder_func,
)
from app.scheduler.tenants import ServedTenant, TenantSource, iter_active_tenants
from app.scheduler.throttle import BotThrottles
from app.services.admin_reminder_service import AdminReminderService
from app.services.quiet_mode_service import deliver_deferred
from app.services.booking_service import complete_past_bookings
from app.services.delivery import DEFAULT_RETRY_AFTER_SECONDS, DeliveryStop, Outcome
from app.services.reminder_service import ReminderService
from app.tenancy import get_owner_telegram_id
from app.utils.timezone import get_master_tz

if TYPE_CHECKING:
    from app.superadmin.alerts import AlertNotifier
    from app.superadmin.digest import TrialDigest

logger = logging.getLogger(__name__)

TenantStep = Callable[[ServedTenant], Awaitable[None]]


class SchedulerService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        source: TenantSource,
        *,
        max_concurrent: int | None = None,
        throttles: BotThrottles | None = None,
        alerts: "AlertNotifier | None" = None,
        digest: "TrialDigest | None" = None,
    ):
        self._session_factory = session_factory
        self._source = source
        self._alerts = alerts  # уведомления суперадмину (этап 9); None — суперадмин-бот выключен
        self._digest = digest  # ежедневная сводка о сроках (этап 10); None — суперадмин-бот выключен
        self._semaphore = asyncio.Semaphore(max_concurrent or settings.scheduler_max_concurrent_tenants)
        self.throttles = throttles or BotThrottles(settings.scheduler_bot_rate_per_second)
        self._scheduler: AsyncIOScheduler | None = None
        self._ticks: set[asyncio.Task] = set()  # выполняющиеся сейчас тики: при остановке дожидаемся их

    # ---- жизненный цикл ----

    def start(self) -> AsyncIOScheduler:
        """Запускает три периодические задачи. Вызывать из работающего event loop."""
        # max_instances=1: тик не накладывается сам на себя; coalesce: пропущенные тики схлопываются в один;
        # misfire_grace_time: тик, опоздавший не больше чем на интервал, всё равно выполняется.
        interval = settings.reminder_poll_interval_seconds
        scheduler = AsyncIOScheduler(
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": interval}
        )
        scheduler.add_job(self.tick_notifications, "interval", seconds=interval, id="reminder_worker")
        scheduler.add_job(self.tick_admin_reminders, "interval", seconds=interval, id="admin_reminder_worker")
        scheduler.add_job(self.tick_completion, "interval", seconds=interval, id="booking_completion_worker")
        if self._digest is not None:
            from app.superadmin.digest import DIGEST_CHECK_INTERVAL_SECONDS

            scheduler.add_job(
                self.tick_trial_digest,
                "interval",
                seconds=DIGEST_CHECK_INTERVAL_SECONDS,
                id="trial_digest_worker",
                misfire_grace_time=DIGEST_CHECK_INTERVAL_SECONDS,
            )
        scheduler.start()
        self._scheduler = scheduler
        return scheduler

    async def stop(self, timeout: float | None = None) -> None:
        """Останавливает планировщик: новые тики не стартуют, текущие отправки доводятся до конца (но не дольше timeout)."""
        timeout = settings.scheduler_shutdown_timeout_seconds if timeout is None else timeout
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        pending = {task for task in self._ticks if not task.done()}
        if not pending:
            return
        logger.info("Остановка планировщика: ждём завершения текущих тиков (%s)", len(pending))
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            task.cancel()
        if still_running:
            # Прерванные отправки не потеряны: уведомления вернутся в очередь по таймауту «зависших» (sending → pending).
            await asyncio.wait(still_running, timeout=5)
            logger.warning("Планировщик остановлен по таймауту, прервано тиков: %s", len(still_running))

    @contextmanager
    def _track_tick(self):
        task = asyncio.current_task()
        if task is not None:
            self._ticks.add(task)
        try:
            yield
        finally:
            if task is not None:
                self._ticks.discard(task)

    # ---- тики (zero-arg корутины: их вызывает APScheduler, а тесты — напрямую) ----

    async def tick_notifications(self) -> None:
        await self._tick("notifications", self._step_notifications, needs_bot=True)

    async def tick_admin_reminders(self) -> None:
        await self._tick("admin_reminders", self._step_admin_reminders, needs_bot=True)

    async def tick_completion(self) -> None:
        await self._tick("completion", self._step_completion, needs_bot=False)

    async def tick_trial_digest(self) -> None:
        """Сводка суперадминам о сроках (этап 10). Сама решает, пора ли слать; сбой одного суперадмина другим не мешает."""
        if self._digest is None:
            return
        with self._track_tick():
            async with tick_lock(self._session_factory, "trial_digest") as acquired:
                if not acquired:
                    logger.info("Планировщик (trial_digest): тик выполняется другим процессом, пропущен")
                    return
                await self._digest.run_once()

    async def _tick(self, name: str, step: TenantStep, *, needs_bot: bool) -> None:
        with self._track_tick():
            # Этап 12C: пока этот же тик выполняется в другом процессе (перекрытие старого и нового контейнера), пропускаем его.
            async with tick_lock(self._session_factory, name) as acquired:
                if not acquired:
                    logger.info("Планировщик (%s): тик выполняется другим процессом, пропущен", name)
                    return
                try:
                    tenants = await iter_active_tenants(self._source)
                except Exception:
                    logger.exception("Планировщик (%s): не удалось получить список мастеров, тик пропущен", name)
                    return
                self.throttles.forget_except({tenant.master_id for tenant in tenants})
                await asyncio.gather(*(self._run_tenant(name, tenant, step, needs_bot) for tenant in tenants))

    # ---- обработка одного мастера: ничего отсюда не выбрасывается ----

    async def _run_tenant(self, name: str, tenant: ServedTenant, step: TenantStep, needs_bot: bool) -> None:
        async with self._semaphore:
            with update_log_context(bot_id=tenant.telegram_bot_id) as log_context:
                log_context.master_id = tenant.master_id
                if needs_bot and self.throttles.for_master(tenant.master_id).is_paused:
                    logger.info("Планировщик (%s): отправка этим ботом приостановлена, мастер пропущен до следующего тика", name)
                    return
                try:
                    await asyncio.wait_for(step(tenant), timeout=settings.scheduler_tenant_timeout_seconds)
                except DeliveryStop as stop:
                    await self._on_delivery_stop(name, tenant, stop)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    logger.error("Планировщик (%s): обработка мастера превысила лимит времени, продолжим на следующем тике", name)
                except Exception:
                    logger.exception("Планировщик (%s): сбой при обработке мастера", name)

    async def _on_delivery_stop(self, name: str, tenant: ServedTenant, stop: DeliveryStop) -> None:
        throttle = self.throttles.for_master(tenant.master_id)
        if stop.outcome is Outcome.STOP_UNAUTHORIZED:
            # Бот неактивен: дальше мастер не обслуживается, пока оператор не подключит бота заново (remove-bot → add-bot).
            # Суперадмину уходит один сигнал на бота (этап 9; повтор не чаще SUPERADMIN_ALERT_INTERVAL_SECONDS).
            logger.error(
                "Токен бота мастера недействителен (Telegram: Unauthorized): бот помечен неактивным, отправка остановлена "
                "до переподключения бота (master_id=%s, bot_id=%s)",
                tenant.master_id,
                tenant.telegram_bot_id,
            )
            try:
                await self._source.deactivate(tenant)
            except Exception:
                logger.exception("Не удалось пометить бота мастера неактивным (master_id=%s)", tenant.master_id)
            await self._alert_invalid_token(tenant)
            return
        delay = stop.retry_after if stop.retry_after is not None else DEFAULT_RETRY_AFTER_SECONDS
        throttle.pause(delay)
        reason = "Telegram просит подождать" if stop.outcome is Outcome.STOP_RETRY_AFTER else "временная ошибка сети/Telegram"
        logger.warning(
            "Планировщик (%s): %s, отправка этим ботом отложена на %.0f с (уведомления остаются в очереди)", name, reason, delay
        )

    async def _alert_invalid_token(self, tenant: ServedTenant) -> None:
        if self._alerts is None:
            return
        from app.superadmin.alerts import KIND_INVALID_TOKEN, invalid_token_text

        try:
            await self._alerts.notify(
                KIND_INVALID_TOKEN,
                tenant.telegram_bot_id if tenant.telegram_bot_id is not None else tenant.master_id,
                invalid_token_text(tenant.master_id, tenant.telegram_bot_id),
            )
        except Exception:
            logger.exception("Не удалось уведомить суперадмина о недействительном токене (master_id=%s)", tenant.master_id)

    # ---- шаги: per-master логика, как и раньше, но для переданного мастера ----

    async def _step_notifications(self, tenant: ServedTenant) -> None:
        master_id, bot = tenant.master_id, tenant.bot
        limiter = self.throttles.for_master(master_id).limiter
        send_funcs_by_type = {
            NotificationType.REMINDER_2H_CLIENT.value: make_send_client_reminder_2h_func(
                bot, self._session_factory, master_id, limiter=limiter
            ),
            NotificationType.REMINDER_2H_MASTER.value: make_send_master_reminder_2h_func(
                bot, self._session_factory, master_id, limiter=limiter
            ),
        }
        send_func = make_send_reminder_func(bot, self._session_factory, master_id, limiter=limiter)
        async with self._session_factory() as session:
            service = ReminderService(session, send_func, send_funcs_by_type, master_id=master_id)
            sent = await service.process_due_notifications(now=dt.datetime.now(dt.timezone.utc))
        if sent:
            logger.info("Sent %s reminders", sent)

    async def _step_admin_reminders(self, tenant: ServedTenant) -> None:
        master_id = tenant.master_id
        async with self._session_factory() as session:
            # Владельца читаем на каждом тике: его могли сменить, пока процесс работает.
            owner_telegram_id = await get_owner_telegram_id(session, master_id)
            if not owner_telegram_id:
                return  # напоминания остаются в БД и уйдут, когда у мастера появится владелец
            limiter = self.throttles.for_master(master_id).limiter
            # Сообщения, накопленные за тихий режим, уходят сразу после его окончания — раньше новых напоминаний.
            # Здесь же, а не отдельной задачей: это тот же шаг «сообщения самому мастеру».
            delivered = await deliver_deferred(session, master_id, make_send_deferred_func(tenant.bot, owner_telegram_id, limiter=limiter))
            if delivered:
                logger.info("Sent %s messages deferred by quiet mode", delivered)
            send = make_send_admin_reminder_func(
                tenant.bot, owner_telegram_id, limiter=limiter, session_factory=self._session_factory, master_id=master_id
            )
            tz = await get_master_tz(session, master_id)
            sent = await AdminReminderService(session, tz, master_id=master_id).process_due(send)
        if sent:
            logger.info("Sent %s admin reminders", sent)

    async def _step_completion(self, tenant: ServedTenant) -> None:
        master_id = tenant.master_id
        async with self._session_factory() as session:
            tz = await get_master_tz(session, master_id)  # «сейчас» — по часам мастера, а не процесса
            completed = await complete_past_bookings(session, master_id, dt.datetime.now(tz))
        if completed:
            logger.info("Marked %s bookings as completed", completed)
