import datetime as dt
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.database.models import Booking, BookingStatus, Notification, NotificationStatus
from app.database.repositories.master_settings_repository import MasterSettingsRepository
from app.services.delivery import DeliveryStop, Outcome, classify_send_error, stop_from

logger = logging.getLogger(__name__)


class ReminderService:
    """Находит due-напоминания и отправляет их. Идемпотентно — safe при повторном запуске
    после рестарта, потому что источник истины — таблица notifications, а не память процесса.

    Жизненный цикл уведомления (этап 6): pending → sending → sent | failed | cancelled.

    1. Забор: `SELECT … FOR UPDATE SKIP LOCKED` + перевод в `sending` + коммит — короткая транзакция. Параллельный тик
       (в этом процессе или в другом) пропускает залоченные и уже взятые строки, поэтому одно уведомление берёт ровно один.
    2. Отправка в Telegram идёт ВНЕ транзакции БД (между коммитами соединение не занято).
    3. Итог в короткой транзакции: sent / failed (не доставить никогда или непонятная ошибка) / обратно в pending
       (бот временно недоступен — см. app.services.delivery) и `DeliveryStop` наверх.
    Зависшее в `sending` дольше `stale_after` (процесс упал посреди отправки) возвращается в pending при следующем тике.
    Гарантия — «не больше одного раза» при штатной работе и «хотя бы раз» после падения процесса на отправке.
    """

    def __init__(
        self,
        session: AsyncSession,
        send_message_func,
        send_funcs_by_type: dict | None = None,
        *,
        master_id: int,
        batch_size: int | None = None,
        stale_after: dt.timedelta | None = None,
    ):
        """send_message_func(booking: Booking) -> Awaitable[None]
        Функция реальной отправки в Telegram — передаётся снаружи, чтобы сервис
        был тестируемым без реального Bot API.

        send_funcs_by_type — необязательный словарь {тип уведомления: функция отправки}
        для уведомлений, которые уходят не по умолчанию (например, напоминание мастеру).
        Для типов, которых в словаре нет, используется send_message_func.
        """
        self.session = session
        self.master_id = master_id
        self.send_message_func = send_message_func
        self.send_funcs_by_type = send_funcs_by_type or {}
        self.batch_size = batch_size or settings.scheduler_notification_batch_size
        self.stale_after = stale_after or dt.timedelta(seconds=settings.scheduler_claim_stale_seconds)

    async def get_due_notifications(self, now: dt.datetime) -> list[Notification]:
        result = await self.session.execute(
            select(Notification).where(
                Notification.master_id == self.master_id,
                Notification.status == NotificationStatus.PENDING.value,
                Notification.scheduled_at <= now,
            )
        )
        return list(result.scalars().all())

    async def _release_stale(self, now: dt.datetime) -> None:
        """Возвращает в очередь уведомления, зависшие в sending (процесс упал, не закончив отправку)."""
        result = await self.session.execute(
            update(Notification)
            .where(
                Notification.master_id == self.master_id,
                Notification.status == NotificationStatus.SENDING.value,
                Notification.claimed_at <= now - self.stale_after,
            )
            .values(status=NotificationStatus.PENDING.value, claimed_at=None)
        )
        await self.session.commit()
        if result.rowcount:
            logger.warning("Возвращено в очередь уведомлений, зависших в sending: %s", result.rowcount)

    async def _claim_due(self, now: dt.datetime) -> list[int]:
        """Атомарно забирает до batch_size созревших уведомлений мастера (pending → sending). Возвращает их id."""
        ids = list(
            (
                await self.session.execute(
                    select(Notification.id)
                    .where(
                        Notification.master_id == self.master_id,
                        Notification.status == NotificationStatus.PENDING.value,
                        Notification.scheduled_at <= now,
                    )
                    .order_by(Notification.scheduled_at, Notification.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if ids:
            await self.session.execute(
                update(Notification)
                .where(Notification.master_id == self.master_id, Notification.id.in_(ids))
                .values(status=NotificationStatus.SENDING.value, claimed_at=now)
            )
        await self.session.commit()
        return ids

    async def _finish(self, notification_id: int, status: NotificationStatus, *, sent_at: dt.datetime | None = None) -> None:
        values: dict = {"status": status.value, "claimed_at": None}
        if sent_at is not None:
            values["sent_at"] = sent_at
        await self.session.execute(
            update(Notification)
            .where(
                Notification.master_id == self.master_id,
                Notification.id == notification_id,
                Notification.status == NotificationStatus.SENDING.value,
            )
            .values(**values)
        )
        await self.session.commit()

    async def _release(self, notification_ids: list[int]) -> None:
        """Возвращает взятые, но не отправленные уведомления в очередь (pending)."""
        if not notification_ids:
            return
        await self.session.execute(
            update(Notification)
            .where(
                Notification.master_id == self.master_id,
                Notification.id.in_(notification_ids),
                Notification.status == NotificationStatus.SENDING.value,
            )
            .values(status=NotificationStatus.PENDING.value, claimed_at=None)
        )
        await self.session.commit()

    async def process_due_notifications(self, now: dt.datetime | None = None) -> int:
        """Отправляет созревшие уведомления мастера, возвращает число отправленных.

        Бросает `DeliveryStop`, если бот мастера сейчас не может отправлять (токен недействителен, лимит Telegram, сеть):
        текущее и ещё не обработанные уведомления к этому моменту уже вернулись в очередь.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        await self._release_stale(now)
        claimed = await self._claim_due(now)
        sent_count = 0

        for position, notification_id in enumerate(claimed):
            try:
                if await self._process_one(notification_id, now):
                    sent_count += 1
            except DeliveryStop:
                await self._release(claimed[position + 1 :])  # текущее вернул _process_one
                raise
            # Отмена задачи или непредвиденный сбой: взятое остаётся в sending и вернётся по таймауту (_release_stale).
        return sent_count

    async def _process_one(self, notification_id: int, now: dt.datetime) -> bool:
        """Отправляет одно взятое уведомление. True — отправлено."""
        notification = (
            await self.session.execute(
                select(Notification).where(Notification.master_id == self.master_id, Notification.id == notification_id)
            )
        ).scalar_one_or_none()
        if notification is None or notification.status != NotificationStatus.SENDING.value:
            await self.session.rollback()
            return False  # исчезло или изменилось, пока ждало очереди
        booking = (
            await self.session.execute(
                select(Booking).where(Booking.master_id == self.master_id, Booking.id == notification.booking_id)
            )
        ).scalar_one_or_none()
        notification_type = notification.type
        # Настройки мастера перечитываются в момент отправки: напоминание, которое мастер успел выключить после
        # создания записи, не уходит (статус cancelled). Выключенное можно включить снова — оно снова станет рабочим
        # только для ещё не обработанных уведомлений.
        type_disabled = notification_type in (
            await MasterSettingsRepository(self.session, self.master_id).get_data()
        ).disabled_notification_types()
        if booking is not None:
            self.session.expunge(booking)  # дальше сессия коммитится; запись нужна отправителю «как есть»
        await self.session.commit()  # закрываем транзакцию ДО обращения к Telegram

        if booking is None or booking.status != BookingStatus.CONFIRMED.value or type_disabled:
            # Запись отменена/удалена между планированием и отправкой, либо мастер выключил этот тип напоминаний — не шлём.
            await self._finish(notification_id, NotificationStatus.CANCELLED)
            return False

        send_func = self.send_funcs_by_type.get(notification_type, self.send_message_func)
        try:
            await send_func(booking)
        except Exception as exc:
            outcome = classify_send_error(exc)
            if outcome.stops_bot:
                # Бот сейчас не отправляет — не теряем уведомление, пока оно ещё имеет смысл (запись не началась).
                if booking.start_datetime <= now:
                    logger.warning("Напоминание по записи %s не доставлено вовремя (%s): запись уже началась", booking.id, outcome.value)
                    await self._finish(notification_id, NotificationStatus.FAILED)
                else:
                    await self._release([notification_id])
                raise stop_from(exc, outcome) from None
            if outcome is Outcome.PERMANENT:
                logger.warning("Напоминание по записи %s не доставлено: %s", booking.id, type(exc).__name__)
            else:
                logger.exception("Failed to send reminder for booking %s", booking.id)
            await self._finish(notification_id, NotificationStatus.FAILED)
            return False

        await self._finish(notification_id, NotificationStatus.SENT, sent_at=now)
        return True
