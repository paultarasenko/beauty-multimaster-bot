"""Какие мастера обслуживаются планировщиком на этом тике (этап 6).

Планировщик один на всех мастеров и на КАЖДОМ тике заново спрашивает «кого обслуживать» — поэтому бот, добавленный или
отключённый через CLI, подхватывается/пропускается без перезапуска сервиса.

- `iter_active_tenants(source)` — единственный шлюз выбора мастеров. Подписок и автоматического отключения нет (этап 10):
  мастера включает и отключает только суперадмин (`masters.is_active` / `master_bots.is_active`), срок пробного периода
  выбор не затрагивает, поэтому `is_tenant_serviced` остаётся точкой расширения и всегда возвращает True.
- Источник (`TenantSource`) знает, откуда брать мастеров и их боты: `RegistryTenantSource` — режим webhook (все активные
  боты из BotRegistry, который заново читает master_bots), `SingleTenantSource` — режим polling (один бот и мастер из настроек).
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import Master, MasterBot
from app.multibot.registry import BotRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServedTenant:
    """Обслуживаемый мастер: чей он и через какой бот ему и его клиентам уходят сообщения."""

    master_id: int
    bot: Bot = field(repr=False)
    telegram_bot_id: int | None = None


class TenantSource(Protocol):
    async def candidates(self) -> list[ServedTenant]:
        """Мастера с активным ботом (без проверки подписки и т.п. — это делает шлюз)."""

    async def deactivate(self, tenant: ServedTenant) -> None:
        """Бот мастера больше не может отправлять (недействительный токен): помечает его неактивным."""


async def is_tenant_serviced(tenant: ServedTenant) -> bool:
    """Точка расширения: обслуживаются все (подписок нет, срок пробного периода ничего не отключает — этап 10)."""
    return True


async def iter_active_tenants(source: TenantSource) -> list[ServedTenant]:
    """Мастера, которых планировщик обслуживает на этом тике: активный мастер + активный бот."""
    served = [tenant for tenant in await source.candidates() if await is_tenant_serviced(tenant)]
    return sorted(served, key=lambda tenant: tenant.master_id)


class RegistryTenantSource:
    """Режим webhook: мастера и боты — из BotRegistry; на каждом тике реестр перечитывает master_bots целиком."""

    def __init__(self, registry: BotRegistry):
        self._registry = registry

    async def candidates(self) -> list[ServedTenant]:
        await self._registry.load_all()
        return [
            ServedTenant(master_id=entry.master_id, bot=entry.bot, telegram_bot_id=entry.telegram_bot_id)
            for entry in self._registry.active_entries()
        ]

    async def deactivate(self, tenant: ServedTenant) -> None:
        if tenant.telegram_bot_id is not None:
            await self._registry.deactivate_bot(tenant.telegram_bot_id)


class SingleTenantSource:
    """Режим polling: один бот и один мастер (settings.master_id). Мастер должен быть активен на момент каждого тика."""

    def __init__(self, session_factory: async_sessionmaker, bot: Bot, master_id: int):
        self._session_factory = session_factory
        self._bot = bot
        self._master_id = master_id
        self._token_rejected = False

    async def candidates(self) -> list[ServedTenant]:
        if self._token_rejected:
            return []  # Telegram отверг токен единственного бота: слать нечем, повторять каждый тик бессмысленно
        async with self._session_factory() as session:
            active = (await session.execute(select(Master.is_active).where(Master.id == self._master_id))).scalar_one_or_none()
        if not active:
            return []
        return [ServedTenant(master_id=self._master_id, bot=self._bot, telegram_bot_id=self._bot.id)]

    async def deactivate(self, tenant: ServedTenant) -> None:
        self._token_rejected = True
        # В polling-режиме строки master_bots может и не быть — тогда обновлять нечего.
        async with self._session_factory() as session:
            await session.execute(update(MasterBot).where(MasterBot.master_id == tenant.master_id).values(is_active=False))
            await session.commit()
