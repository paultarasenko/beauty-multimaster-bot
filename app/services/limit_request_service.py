import datetime as dt
import logging
from html import escape

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BookingLimitRequest, LimitRequestStatus
from app.database.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

MAX_REQUEST_LEN = 500  # длина сообщения клиента мастеру и ответа мастера клиенту

APPROVED_TEXT = (
    "✅ Мастер принял ваш запрос.\n\n"
    "Вы можете сделать ещё одну запись сверх лимита — нажмите «📅 Записаться»."
)
REJECTED_TEXT = (
    "❌ Мастер отклонил ваш запрос.\n\n"
    "Чтобы записаться снова, дождитесь визита или отмените одну из записей."
)


async def get_pending_request(session: AsyncSession, master_id: int, user_id: int) -> BookingLimitRequest | None:
    result = await session.execute(
        select(BookingLimitRequest).where(
            BookingLimitRequest.master_id == master_id,
            BookingLimitRequest.user_id == user_id,
            BookingLimitRequest.status == LimitRequestStatus.PENDING.value,
        )
    )
    return result.scalars().first()


async def get_unused_approval(session: AsyncSession, master_id: int, user_id: int) -> BookingLimitRequest | None:
    """Одобренный и ещё не использованный запрос: разовое разрешение на запись сверх лимита."""
    result = await session.execute(
        select(BookingLimitRequest)
        .where(
            BookingLimitRequest.master_id == master_id,
            BookingLimitRequest.user_id == user_id,
            BookingLimitRequest.status == LimitRequestStatus.APPROVED.value,
            BookingLimitRequest.override_used_at.is_(None),
        )
        .order_by(BookingLimitRequest.id)
        .limit(1)
    )
    return result.scalars().first()


async def burn_unused_approvals(session: AsyncSession, master_id: int, user_id: int) -> None:
    """Сжигает неиспользованные разрешения на запись сверх лимита.

    Вызывается, когда мастер ограничивает клиента или блокирует его навсегда: после снятия ограничения
    клиент возвращается к обычному лимиту, а старое «Принять» уже не действует (override_used_at
    заполняется так же, как при использовании разрешения)."""
    await session.execute(
        update(BookingLimitRequest)
        .where(
            BookingLimitRequest.master_id == master_id,
            BookingLimitRequest.user_id == user_id,
            BookingLimitRequest.status == LimitRequestStatus.APPROVED.value,
            BookingLimitRequest.override_used_at.is_(None),
        )
        .values(override_used_at=dt.datetime.now(dt.timezone.utc))
    )


async def create_request(
    session: AsyncSession, master_id: int, user_id: int, message: str
) -> BookingLimitRequest | None:
    """Создаёт запрос. None — у клиента уже есть ожидающий запрос (в том числе при гонке двойного нажатия)."""
    if await get_pending_request(session, master_id, user_id) is not None:
        return None
    request = BookingLimitRequest(master_id=master_id, user_id=user_id, message=message)
    session.add(request)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None
    return request


async def _get_request_for_update(session: AsyncSession, master_id: int, request_id: int) -> BookingLimitRequest | None:
    """Запрос только своего мастера, с блокировкой строки."""
    result = await session.execute(
        select(BookingLimitRequest)
        .where(BookingLimitRequest.master_id == master_id, BookingLimitRequest.id == request_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_request(session: AsyncSession, master_id: int, request_id: int) -> BookingLimitRequest | None:
    """Запрос только своего мастера (id приходит из callback_data — его можно подделать)."""
    result = await session.execute(
        select(BookingLimitRequest).where(
            BookingLimitRequest.master_id == master_id, BookingLimitRequest.id == request_id
        )
    )
    return result.scalar_one_or_none()


async def decide_request(
    session: AsyncSession, master_id: int, request_id: int, approve: bool
) -> BookingLimitRequest | None:
    """Фиксирует решение мастера. None — запроса нет или он уже обработан (повторное нажатие кнопки)."""
    request = await _get_request_for_update(session, master_id, request_id)
    if request is None or request.status != LimitRequestStatus.PENDING.value:
        return None
    request.status = (LimitRequestStatus.APPROVED if approve else LimitRequestStatus.REJECTED).value
    request.decided_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()
    return request


def build_client_text(request: BookingLimitRequest, comment: str | None) -> str:
    text = APPROVED_TEXT if request.status == LimitRequestStatus.APPROVED.value else REJECTED_TEXT
    if comment:
        text += f"\n\n💬 Сообщение от мастера:\n{escape(comment)}"
    return text


async def deliver_decision(
    bot: Bot, session: AsyncSession, master_id: int, request_id: int, comment: str | None = None
) -> bool:
    """Отправляет клиенту решение мастера (с текстом мастера или без) ровно один раз.

    True — клиент уведомлён (или уже был уведомлён раньше), False — доставить не удалось.
    """
    request = await _get_request_for_update(session, master_id, request_id)
    if request is None or request.status == LimitRequestStatus.PENDING.value:
        return False
    if request.client_notified_at is not None:
        return True
    client = await UserRepository(session, master_id).get_by_id(request.user_id)
    if client is None:
        return False
    try:
        await bot.send_message(client.telegram_id, build_client_text(request, comment))
    except Exception:
        logger.exception("Не удалось отправить клиенту решение мастера (request_id=%s)", request_id)
        return False
    request.master_comment = comment
    request.client_notified_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()
    return True
