"""Учёт временных сообщений чата: хранилище, контекст апдейта, перехват отправок (лёгкий модуль без зависимостей от бота).

Подключается к HTTP-сессии Bot в `app/multibot/telegram_api.py`; правила очистки — в `app/bot/screens.py`.
"""

import contextlib
import json
import logging
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Message, ReplyKeyboardMarkup
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "bbot:screens"
STORE_TTL_SECONDS = 47 * 3600  # Telegram не даёт удалять сообщения старше 48 часов
MAX_TRACKED_PER_CHAT = 60
MAX_TRACKED_CHATS = 20_000



# ---------- хранилище запомненных сообщений ----------

Entry = list  # [message_id: int, holds_keyboard: bool]


class ScreenStore:
    """id временных сообщений чата. Redis (общий, переживает перезапуск) или память процесса — по режиму FSM."""

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis
        self._memory: dict[tuple[int, int], list[Entry]] = {}

    @classmethod
    def for_storage(cls, storage: BaseStorage) -> "ScreenStore":
        return cls(storage.redis if isinstance(storage, RedisStorage) else None)

    @staticmethod
    def _key(bot_id: int, chat_id: int) -> str:
        return f"{KEY_PREFIX}:{bot_id}:{chat_id}"

    async def load(self, bot_id: int, chat_id: int) -> list[Entry]:
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._key(bot_id, chat_id))
                return [[int(i), bool(k)] for i, k in json.loads(raw)] if raw else []
            except Exception as exc:
                logger.warning("Экраны чата: не удалось прочитать Redis (%s)", type(exc).__name__)
                return []
        return [list(e) for e in self._memory.get((bot_id, chat_id), [])]

    async def save(self, bot_id: int, chat_id: int, entries: list[Entry]) -> None:
        entries = entries[-MAX_TRACKED_PER_CHAT:]
        if self._redis is not None:
            try:
                key = self._key(bot_id, chat_id)
                if entries:
                    await self._redis.set(key, json.dumps(entries), ex=STORE_TTL_SECONDS)
                else:
                    await self._redis.delete(key)
            except Exception as exc:
                logger.warning("Экраны чата: не удалось записать в Redis (%s)", type(exc).__name__)
            return
        if not entries:
            self._memory.pop((bot_id, chat_id), None)
            return
        if len(self._memory) >= MAX_TRACKED_CHATS and (bot_id, chat_id) not in self._memory:
            self._memory.pop(next(iter(self._memory)))  # забываем самый старый чат
        self._memory[(bot_id, chat_id)] = entries


# ---------- контекст одного апдейта ----------


@dataclass
class _Scope:
    bot_id: int
    chat_id: int
    new: list[Entry] = field(default_factory=list)
    prune: bool = False


_scope: ContextVar[_Scope | None] = ContextVar("screens_scope", default=None)
_untracked: ContextVar[bool] = ContextVar("screens_untracked", default=False)


def prune_previous_screens() -> None:
    """Просит убрать прежние временные сообщения чата после обработки этого апдейта — как при переходе по кнопке меню.

    Для inline-кнопки, которая открывает новый раздел (например, «Записаться» под услугой): обычное нажатие inline-кнопки
    переходом не считается, и прежние экраны остались бы. Удаление идёт уже после того, как новый экран показан."""
    scope = _scope.get()
    if scope is not None:
        scope.prune = True


@contextlib.contextmanager
def untracked() -> Iterator[None]:
    """Отправки внутри блока НЕ считаются временными экранами (уведомления мастеру о записях и т.п.).

    Нужно, когда получатель совпадает с автором апдейта: например, мастер проверяет бота сам и записывается как клиент —
    его собственное уведомление о новой записи не должно пропасть при следующем переходе."""
    token = _untracked.set(True)
    try:
        yield
    finally:
        _untracked.reset(token)


class ScreenTrackingRequestMiddleware(BaseRequestMiddleware):
    """Подключается к HTTP-сессии Bot (`telegram_session()`): запоминает сообщения, которые бот отправил в чат автора
    текущего апдейта. Правки (edit_*) не запоминаются — id экрана уже известен с момента отправки."""

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        try:
            scope = _scope.get()
            if scope is not None and not _untracked.get() and type(method).__name__.startswith("Send"):
                holds_keyboard = isinstance(getattr(method, "reply_markup", None), ReplyKeyboardMarkup)
                for message in result if isinstance(result, list) else [result]:
                    if isinstance(message, Message) and message.chat.id == scope.chat_id and bot.id == scope.bot_id:
                        scope.new.append([message.message_id, holds_keyboard])
        except Exception:  # учёт сообщений вторичен: он не должен ломать отправку
            logger.debug("Экраны чата: не удалось запомнить сообщение", exc_info=True)
        return result


screen_tracking = ScreenTrackingRequestMiddleware()
