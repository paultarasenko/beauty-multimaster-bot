"""Владелец мастера: права, получатель уведомлений и начальная загрузка из ADMIN_TELEGRAM_ID.

Источник прав — `masters.owner_telegram_id`. Настройка `ADMIN_TELEGRAM_ID` используется только в
`bootstrap_owner` (один раз при старте, если у мастера владелец ещё не задан) и больше нигде.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import settings
from app.database.models import Master

logger = logging.getLogger(__name__)


def is_owner(master: Master, telegram_id: int | None) -> bool:
    """Telegram-аккаунт `telegram_id` — владелец именно этого мастера. Владелец мастера A не владелец мастера B;
    у мастера без владельца (owner_telegram_id пуст) владельца нет ни у кого."""
    return telegram_id is not None and master.owner_telegram_id is not None and telegram_id == master.owner_telegram_id


async def get_owner_telegram_id(session: AsyncSession, master_id: int) -> int | None:
    """Актуальный owner_telegram_id мастера из БД (для долгоживущих процессов вроде планировщика: владельца могли сменить)."""
    return (await session.execute(select(Master.owner_telegram_id).where(Master.id == master_id))).scalar_one_or_none()


async def bootstrap_owner(session_factory: async_sessionmaker) -> int | None:
    """Начальная загрузка владельца при старте: если у мастера settings.master_id владелец не задан, а ADMIN_TELEGRAM_ID
    задан — записывает его в masters.owner_telegram_id. Уже заданного владельца значение из .env НЕ перезаписывает.
    Если владельца нет нигде — предупреждение в лог, приложение продолжает работу. Возвращает итоговый owner_telegram_id.
    """
    async with session_factory() as session:
        master = await session.get(Master, settings.master_id)
        if master is None:
            return None  # отсутствие мастера обрабатывает ensure_tenant_available
        if master.owner_telegram_id is not None:
            if settings.admin_telegram_id and settings.admin_telegram_id != master.owner_telegram_id:
                logger.info(
                    "Владелец мастера id=%s уже задан в БД (%s): ADMIN_TELEGRAM_ID из .env игнорируется",
                    master.id,
                    master.owner_telegram_id,
                )
            return master.owner_telegram_id
        if settings.admin_telegram_id:
            master.owner_telegram_id = settings.admin_telegram_id
            await session.commit()
            logger.info("Владелец мастера id=%s задан из ADMIN_TELEGRAM_ID: %s", master.id, master.owner_telegram_id)
            return master.owner_telegram_id
        logger.warning(
            "У мастера id=%s не задан владелец (masters.owner_telegram_id пуст, ADMIN_TELEGRAM_ID не указан): "
            "админ-панель недоступна, уведомления мастеру не отправляются",
            master.id,
        )
        return None
