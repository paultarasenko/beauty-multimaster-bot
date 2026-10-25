"""Реестр ботов (BotRegistry): master_bots → Bot'ы в памяти процесса.

Один процесс обслуживает всех мастеров: реестр создаёт и кэширует Bot для каждого активного master_bots, находит бота
и мастера по пути webhook и умеет добавить/убрать/обновить бота без перезапуска. CLI работает в другом процессе, поэтому
запись кэша живёт ограниченное время (`ttl`): после него она перечитывается из БД, а неизвестный бот ищется в БД лениво.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import Master, MasterBot
from app.multibot.crypto import TokenCipher, TokenDecryptionError
from app.multibot.telegram_api import telegram_session

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30.0  # через сколько запись кэша перечитывается из БД (изменения от CLI подхватываются за это время)
DEFAULT_NEGATIVE_TTL_SECONDS = 5.0  # как долго помним «такого бота нет», чтобы перебор id не нагружал БД
MAX_NEGATIVE_CACHE = 1024


@dataclass
class BotEntry:
    """Запись реестра. Токена здесь нет (он только внутри Bot), а секреты скрыты из repr."""

    telegram_bot_id: int
    master_id: int
    bot_username: str | None
    is_active: bool  # master_bots.is_active
    master_active: bool  # masters.is_active на момент загрузки (актуальность проверяет ещё и TenantMiddleware)
    webhook_secret: str = field(repr=False)
    header_token: str = field(repr=False)
    bot: Bot | None = field(repr=False)  # None, если бот не активен или токен не расшифровался
    loaded_at: float = field(default_factory=time.monotonic, repr=False)

    @property
    def accepts_updates(self) -> bool:
        return self.is_active and self.master_active and self.bot is not None


def _default_session_factory() -> BaseSession:
    return telegram_session()


class BotRegistry:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        cipher: TokenCipher,
        *,
        http_session_factory: Callable[[], BaseSession] = _default_session_factory,
        ttl: float = DEFAULT_TTL_SECONDS,
        negative_ttl: float = DEFAULT_NEGATIVE_TTL_SECONDS,
    ):
        self._session_factory = session_factory
        self._cipher = cipher
        self._http_session_factory = http_session_factory
        self._http_session: BaseSession | None = None  # общая HTTP-сессия всех Bot'ов; закрывается в close()
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._entries: dict[int, BotEntry] = {}
        self._missing: dict[int, float] = {}
        self._closed = False

    # ---- служебное ----

    def _shared_http_session(self) -> BaseSession:
        if self._http_session is None:
            self._http_session = self._http_session_factory()
        return self._http_session

    def _make_bot(self, token: str) -> Bot:
        return Bot(
            token=token,
            session=self._shared_http_session(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

    def _build_entry(self, row: MasterBot, master_active: bool, previous: BotEntry | None) -> BotEntry:
        bot: Bot | None = None
        if row.is_active:
            try:
                token = self._cipher.decrypt(row.bot_token_encrypted)
            except TokenDecryptionError:
                logger.error("Бот %s (master_id=%s): токен не расшифровывается, бот отключён в реестре", row.telegram_bot_id, row.master_id)
            else:
                # Тот же токен — тот же объект Bot (стабильная идентичность между обновлениями записи).
                if previous is not None and previous.bot is not None and previous.bot.token == token:
                    bot = previous.bot
                else:
                    bot = self._make_bot(token)
        return BotEntry(
            telegram_bot_id=row.telegram_bot_id,
            master_id=row.master_id,
            bot_username=row.bot_username,
            is_active=row.is_active,
            master_active=master_active,
            webhook_secret=row.webhook_secret,
            header_token=self._cipher.header_token(row.webhook_secret),
            bot=bot,
        )

    async def _fetch(self, where) -> list[tuple[MasterBot, bool]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MasterBot, Master.is_active).join(Master, Master.id == MasterBot.master_id).where(*where)
            )
            return [(row, master_active) for row, master_active in result.all()]

    # ---- загрузка ----

    async def load_all(self) -> int:
        """Полная перезагрузка из БД (старт процесса). Возвращает число ботов в реестре."""
        rows = await self._fetch([])
        fresh: dict[int, BotEntry] = {}
        for row, master_active in rows:
            fresh[row.telegram_bot_id] = self._build_entry(row, master_active, self._entries.get(row.telegram_bot_id))
        self._entries = fresh
        self._missing.clear()
        return len(fresh)

    reload_all = load_all

    async def refresh(self, telegram_bot_id: int) -> BotEntry | None:
        """Перечитывает одного бота из БД: появился/изменился — обновляет запись, исчез — убирает."""
        rows = await self._fetch([MasterBot.telegram_bot_id == telegram_bot_id])
        if not rows:
            self._entries.pop(telegram_bot_id, None)
            self._remember_missing(telegram_bot_id)
            return None
        row, master_active = rows[0]
        entry = self._build_entry(row, master_active, self._entries.get(telegram_bot_id))
        self._entries[telegram_bot_id] = entry
        self._missing.pop(telegram_bot_id, None)
        return entry

    def _remember_missing(self, telegram_bot_id: int) -> None:
        if len(self._missing) >= MAX_NEGATIVE_CACHE:
            self._missing.clear()
        self._missing[telegram_bot_id] = time.monotonic()

    async def get(self, telegram_bot_id: int) -> BotEntry | None:
        """Запись бота по id в Telegram. Свежую берёт из кэша, протухшую или неизвестную — перечитывает из БД."""
        now = time.monotonic()
        entry = self._entries.get(telegram_bot_id)
        if entry is not None:
            if now - entry.loaded_at < self._ttl:
                return entry
            return await self.refresh(telegram_bot_id)
        missed_at = self._missing.get(telegram_bot_id)
        if missed_at is not None and now - missed_at < self._negative_ttl:
            return None
        return await self.refresh(telegram_bot_id)

    # ---- управление без перезапуска ----

    def put(self, row: MasterBot, master_active: bool = True) -> BotEntry:
        """Добавляет/обновляет бота в кэше по готовой строке master_bots (когда добавление произошло в этом же процессе)."""
        entry = self._build_entry(row, master_active, self._entries.get(row.telegram_bot_id))
        self._entries[row.telegram_bot_id] = entry
        self._missing.pop(row.telegram_bot_id, None)
        return entry

    async def deactivate_bot(self, telegram_bot_id: int) -> bool:
        """Помечает бота неактивным в БД (Telegram отверг токен) и сразу обновляет запись реестра. True — бот найден.

        Снова подключить мастеру бота можно только вручную (CLI `remove-bot`, затем `add-bot` с новым токеном): это осознанное действие оператора.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                update(MasterBot).where(MasterBot.telegram_bot_id == telegram_bot_id).values(is_active=False)
            )
            await session.commit()
        await self.refresh(telegram_bot_id)
        return result.rowcount > 0

    def remove(self, telegram_bot_id: int) -> bool:
        """Убирает бота из кэша. Общая HTTP-сессия остаётся открытой (она общая для всех ботов)."""
        return self._entries.pop(telegram_bot_id, None) is not None

    # ---- чтение для остальных частей приложения ----

    def master_id_for_bot(self, bot: Bot) -> int | None:
        """Мастер бота, принявшего апдейт (для resolve_tenant). Берёт запись из кэша; чужой/неизвестный Bot → None."""
        entry = self._entries.get(bot.id)
        if entry is None or entry.bot is not bot:
            return None
        return entry.master_id

    def active_entries(self) -> list[BotEntry]:
        """Боты, которые принимают апдейты (активный бот, активный мастер, токен расшифрован)."""
        return [e for e in self._entries.values() if e.accepts_updates]

    def __len__(self) -> int:
        return len(self._entries)

    async def close(self) -> None:
        """Закрывает общую HTTP-сессию и очищает кэш. Повторный вызов безопасен."""
        self._entries.clear()
        self._missing.clear()
        if self._http_session is not None:
            session, self._http_session = self._http_session, None
            await session.close()
        self._closed = True
