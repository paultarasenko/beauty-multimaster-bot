"""Тихий режим мастера: пока он действует, уведомления мастеру не отправляются, а откладываются в БД и уходят после его окончания.

Это настройка бота, а не беззвучный режим Telegram: сообщения не «приходят без звука», а не приходят вовсе, пока окно не
закончится. Под режим попадают только сообщения мастеру (новые записи, отмены, запросы клиентов, напоминания мастеру и
личные напоминания). Клиентов он не касается. Ответы бота на собственные действия мастера в меню не откладываются.

Окно — ежедневное, по часам мастера (`master_profile.timezone`), может переходить через полночь.
"""

import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.quiet_mode_repository import DeferredMessageRepository, QuietModeRepository
from app.services.delivery import Outcome, classify_send_error, stop_from
from app.utils.quiet_mode import QuietModeData, is_quiet_time
from app.utils.timezone import get_master_tz

logger = logging.getLogger(__name__)

DELIVERY_BATCH_LIMIT = 50  # сколько отложенных сообщений отправляется за один тик; остальное уйдёт на следующем

SendDeferred = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]


async def load_quiet_mode(session: AsyncSession, master_id: int) -> QuietModeData:
    return await QuietModeRepository(session, master_id).get_data()


async def is_quiet_now(session: AsyncSession, master_id: int, now: dt.datetime | None = None) -> bool:
    quiet = await load_quiet_mode(session, master_id)
    if not quiet.enabled:
        return False  # часовой пояс не читаем: у большинства мастеров режим выключен
    tz = await get_master_tz(session, master_id)
    local_time = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz).time()
    return is_quiet_time(quiet.start_time, quiet.end_time, local_time)


async def defer_if_quiet(
    session: AsyncSession,
    master_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    expires_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Если у мастера сейчас тихий режим — откладывает сообщение (flush, без commit) и возвращает True."""
    if not await is_quiet_now(session, master_id, now):
        return False
    markup_json = reply_markup.model_dump(mode="json", exclude_none=True) if reply_markup is not None else None
    await DeferredMessageRepository(session, master_id).add(text, markup_json, expires_at)
    return True


def _with_event_time(text: str, created_at: dt.datetime, tz: dt.tzinfo, now: dt.datetime) -> str:
    """Отложенное сообщение приходит позже события, поэтому в начало добавляется, когда оно произошло."""
    local = created_at.astimezone(tz)
    stamp = local.strftime("%H:%M") if local.date() == now.astimezone(tz).date() else local.strftime("%d.%m %H:%M")
    return f"🌙 Тихий режим · {stamp}\n\n{text}"


async def deliver_deferred(
    session: AsyncSession, master_id: int, send: SendDeferred, now: dt.datetime | None = None
) -> int:
    """Отправляет накопленные сообщения мастера (по порядку событий), когда тихого режима уже нет. Возвращает число отправленных.

    Каждое сообщение — в своей транзакции: строка блокируется, уходит в Telegram, затем удаляется (как у личных
    напоминаний). Ошибки (этап 6, app.services.delivery): бот сейчас не может отправлять (токен, лимит Telegram, сеть) —
    откат и `DeliveryStop` наверх, остальное остаётся в очереди; сообщение не доставить никогда (мастер заблокировал бота) или
    непонятная ошибка — сообщение отбрасывается, чтобы не повторяться каждый тик.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if await is_quiet_now(session, master_id, now):
        return 0
    repo = DeferredMessageRepository(session, master_id)
    await repo.delete_expired(now)
    await session.commit()
    tz: dt.tzinfo | None = None
    sent = 0
    for _ in range(DELIVERY_BATCH_LIMIT):
        message = await repo.claim_oldest()
        if message is None:
            await session.commit()
            break
        if tz is None:
            tz = await get_master_tz(session, master_id)
        message_id = message.id
        text = _with_event_time(message.text, message.created_at, tz, now)
        markup = InlineKeyboardMarkup.model_validate(message.reply_markup) if message.reply_markup else None
        try:
            await send(text, markup)
        except Exception as exc:
            outcome = classify_send_error(exc)
            if outcome.stops_bot:
                await session.rollback()
                raise stop_from(exc, outcome) from None
            if outcome is Outcome.PERMANENT:
                logger.warning("Отложенное сообщение мастеру %s не доставлено (%s): отброшено", master_id, type(exc).__name__)
            else:
                logger.exception("Не удалось отправить отложенное сообщение мастеру %s: отброшено", master_id)
            await repo.delete(message_id)
            await session.commit()
            continue
        await repo.delete(message_id)
        await session.commit()
        sent += 1
    return sent
