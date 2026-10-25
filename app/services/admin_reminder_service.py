import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminReminder, Recurrence
from app.database.repositories.admin_reminder_repository import AdminReminderRepository
from app.services.delivery import Outcome, classify_send_error, stop_from
from app.services.exceptions import ReminderInPastError
from app.utils.recurrence import next_occurrence

logger = logging.getLogger(__name__)

SendFunc = Callable[[str], Awaitable[None]]


class AdminReminderService:
    """Личные напоминания мастера: создание, просмотр, удаление и отправка наступивших.

    `tz` — таймзона мастера: дата и время, которые вводит мастер, — это его настенные часы.
    """

    def __init__(self, session: AsyncSession, tz: dt.tzinfo, *, master_id: int):
        self.session = session
        self.tz = tz
        self.master_id = master_id
        self.repo = AdminReminderRepository(session, master_id)

    def first_fire_at(
        self,
        recurrence: Recurrence,
        anchor_date: dt.date,
        remind_time: dt.time,
        now: dt.datetime | None = None,
    ) -> dt.datetime:
        """Когда напоминание сработает в первый раз. ReminderInPastError — «один раз» и время уже прошло."""
        now = now or dt.datetime.now(dt.timezone.utc)
        fire_at = next_occurrence(recurrence, anchor_date, remind_time, self.tz, after=now)
        if fire_at is None:
            raise ReminderInPastError
        return fire_at

    async def create(
        self,
        text: str,
        recurrence: Recurrence,
        anchor_date: dt.date,
        remind_time: dt.time,
        now: dt.datetime | None = None,
    ) -> AdminReminder:
        fire_at = self.first_fire_at(recurrence, anchor_date, remind_time, now)
        return await self.repo.add(text, recurrence.value, anchor_date, remind_time, fire_at)

    async def process_due(self, send: SendFunc, now: dt.datetime | None = None) -> int:
        """Отправляет наступившие напоминания и планирует следующие. Возвращает число отправленных.

        Каждое напоминание — в своей транзакции: сначала атомарно «забираем» его (перенос на следующее
        срабатывание или удаление для одноразового), затем отправляем и коммитим. Если отправка упала —
        откат вернёт всё как было, и напоминание повторится на следующем тике воркера. Параллельный
        воркер не отправит дубль: его «захват» упрётся в заблокированную строку и ничего не найдёт.
        Повторяющееся напоминание после простоя бота срабатывает один раз, а не за каждый пропущенный день.

        Транзакция здесь осознанно охватывает отправку: она держит одну строку и ограничена таймаутом отправки
        (`scheduler_send_timeout_seconds`), зато откат без «компенсирующих» правок (восстановление удалённого
        одноразового напоминания) надёжно возвращает напоминание в очередь. Ошибки отправки (этап 6):
        бот сейчас не может отправлять (токен, лимит Telegram, сеть) — откат и `DeliveryStop` наверх, остальные
        напоминания этого бота на тике не трогаем; напоминание не доставить никогда (владелец заблокировал бота) —
        это срабатывание пропускается (захват коммитится, повторяющееся придёт в следующий раз), без повторов каждый тик.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        # После rollback ORM-объекты истекают, а в async-сессии ленивая подгрузка невозможна —
        # поэтому работаем с простыми значениями.
        due = [
            (r.id, r.text, r.recurrence, r.anchor_date, r.remind_time, r.next_fire_at)
            for r in await self.repo.get_due(now)
        ]
        sent = 0
        for reminder_id, text, recurrence, anchor_date, remind_time, fired_at in due:
            next_fire_at = next_occurrence(recurrence, anchor_date, remind_time, self.tz, after=now)
            if next_fire_at is None:
                claimed = await self.repo.delete_if_fired_at(reminder_id, fired_at)
            else:
                claimed = await self.repo.advance(reminder_id, fired_at, next_fire_at)
            if not claimed:
                await self.session.rollback()
                continue
            try:
                await send(text)
                await self.session.commit()
                sent += 1
            except Exception as exc:
                outcome = classify_send_error(exc)
                if outcome is Outcome.PERMANENT:
                    logger.warning(
                        "Напоминание мастера %s не доставлено (%s): это срабатывание пропущено", reminder_id, type(exc).__name__
                    )
                    await self.session.commit()  # захват остаётся: не повторяем недоставляемое каждый тик
                    continue
                await self.session.rollback()
                if outcome.stops_bot:
                    raise stop_from(exc, outcome) from None
                logger.exception("Не удалось отправить напоминание мастера %s", reminder_id)
        return sent
