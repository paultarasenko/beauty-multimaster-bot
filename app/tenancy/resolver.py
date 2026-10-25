"""Определение мастера (арендатора) для апдейта.

`resolve_tenant` — единственное «шов»-место определения мастера.
- webhook (этап 4): в data диспетчера есть `bot_registry` — мастер определяется по боту, принявшему апдейт
  (data["bot"] → реестр → master_id);
- polling: реестра нет, мастер берётся из settings.master_id.
В режиме RUN_MODE=webhook без реестра мастер НЕ подставляется из settings.master_id (иначе апдейт любого бота
обработался бы в данных мастера №1) — апдейт отбрасывается.
"""

from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import settings
from app.database.models import Master
from app.tenancy.context import TenantContext


class TenantNotAvailableError(RuntimeError):
    """Мастер не найден или отключён (is_active = false)."""


class TenantDisabledError(TenantNotAvailableError):
    """Мастер существует, но отключён суперадмином (этап 10): боту можно коротко вежливо ответить (см. app/bot/disabled_replies.py)."""

    def __init__(self, master_id: int):
        super().__init__(f"Мастер id={master_id} отключён (is_active = false)")
        self.master_id = master_id


async def _load_active_master(session: AsyncSession, master_id: int) -> Master:
    master = await session.get(Master, master_id)
    if master is None:
        raise TenantNotAvailableError(f"Мастер id={master_id} не найден")
    if not master.is_active:
        raise TenantDisabledError(master_id)
    return master


async def resolve_tenant(session: AsyncSession, event: TelegramObject, data: dict) -> TenantContext:
    """Мастер для этого апдейта. Бросает TenantNotAvailableError, если мастера нет или он отключён.

    Бот апдейта (`data["bot"]`) в режиме webhook должен быть создан реестром; чужой или неизвестный бот — ошибка.
    """
    registry = data.get("bot_registry")
    if registry is not None:
        bot = data.get("bot")
        master_id = registry.master_id_for_bot(bot) if bot is not None else None
        if master_id is None:
            raise TenantNotAvailableError("Бот апдейта не найден в реестре ботов")
    elif settings.run_mode == "webhook":
        raise TenantNotAvailableError("Режим webhook без реестра ботов: мастер апдейта определить нельзя")
    else:
        master_id = settings.master_id
    master = await _load_active_master(session, master_id)
    return TenantContext(master_id=master.id, master=master, session=session)


async def ensure_tenant_available(session_factory: async_sessionmaker) -> TenantContext:
    """Проверка при старте приложения: мастер из настроек существует и активен. Иначе — понятная ошибка."""
    async with session_factory() as session:
        master = await _load_active_master(session, settings.master_id)
        return TenantContext(master_id=master.id, master=master)
