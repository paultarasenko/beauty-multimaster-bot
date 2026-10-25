"""Настройки мастера и «заводские» данные нового мастера (этап 8)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.database.models import MasterProfile
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.master_settings_repository import MasterSettingsRepository
from app.utils.master_settings import MasterSettingsData


async def load_master_settings(session: AsyncSession, master_id: int) -> MasterSettingsData:
    """Актуальные настройки мастера из БД (без кэша в процессе: реплик может быть несколько)."""
    return await MasterSettingsRepository(session, master_id).get_data()


async def ensure_master_defaults(session: AsyncSession, master_id: int) -> None:
    """Новый мастер «работает из коробки»: создаёт master_profile и master_settings со значениями по умолчанию.

    Существующие строки не трогает (идемпотентно). Часовой пояс нового профиля — settings.timezone (запасной
    вариант из .env, дальше мастер меняет его в профиле). Коммит — на вызывающем.
    """
    content = ContentRepository(session, master_id)
    if await content.get_master_profile() is None:
        session.add(MasterProfile(master_id=master_id, timezone=settings.timezone))
        await session.flush()
    await MasterSettingsRepository(session, master_id).ensure_row()
