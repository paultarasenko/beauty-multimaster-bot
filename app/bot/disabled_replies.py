"""Вежливые ответы отключённому боту (этап 10): вместо молчания — короткая фраза, но не чаще раза в несколько часов.

Апдейт по-прежнему НЕ доходит до хендлеров: бот только коротко отвечает и больше ничего не делает (без кнопок, без доступа к
функциям). Записи и данные мастера при отключении сохраняются.

- клиент (любой, кроме владельца мастера) получает «запись у этого мастера временно недоступна, свяжитесь с мастером напрямую»;
- владелец мастера — «бот временно отключён, обратитесь к администратору сервиса» (без технических подробностей).

Антиспам — на пару (бот, пользователь), интервал `REPLY_INTERVAL_SECONDS` (6 часов). Хранилище следует режиму FSM_STORAGE: при Redis —
ключ `bbot:disabled_reply:<bot_id>:<user_id>` с TTL (общий для процессов и переживает перезапуск; `SET NX EX`), при memory — словарь
в памяти процесса (после перезапуска ответ возможен снова). Если Redis недоступен — не отвечаем (лучше молчание, чем спам).

Ответить технически можно, только если у процесса есть живой Bot: мастер отключён, а его бот активен. Если выключен сам бот
(master_bots.is_active = false) или токен не расшифровывается, Bot в реестре не создаётся — апдейт молча отбрасывается (200).
"""

import logging
import time
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CLIENT_TEXT = "Запись у этого мастера временно недоступна. Пожалуйста, свяжитесь с мастером напрямую."
OWNER_TEXT = "Бот временно отключён. Обратитесь к администратору сервиса."

REPLY_INTERVAL_SECONDS = 6 * 3600
KEY_PREFIX = "bbot:disabled_reply"
MAX_TRACKED_USERS = 50_000
SEND_TIMEOUT_SECONDS = 10.0


class DisabledReplyThrottle:
    """«Можно ли ответить этому человеку в этом боте сейчас». Redis (общий) или память процесса — по режиму FSM."""

    def __init__(
        self,
        redis: Redis | None = None,
        *,
        interval_seconds: int = REPLY_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._redis = redis
        self._interval = interval_seconds
        self._clock = clock
        self._last: dict[tuple[int, int], float] = {}

    @classmethod
    def for_storage(cls, storage: BaseStorage) -> "DisabledReplyThrottle":
        return cls(storage.redis if isinstance(storage, RedisStorage) else None)

    async def allow(self, bot_id: int, user_id: int) -> bool:
        """True — ответить можно (и это зафиксировано); False — недавно уже отвечали (или хранилище недоступно)."""
        if self._redis is not None:
            try:
                return bool(await self._redis.set(f"{KEY_PREFIX}:{bot_id}:{user_id}", "1", nx=True, ex=self._interval))
            except Exception as exc:
                logger.warning("Антиспам ответов отключённому боту: Redis недоступен (%s), ответ пропущен", type(exc).__name__)
                return False
        now = self._clock()
        last = self._last.get((bot_id, user_id))
        if last is not None and now - last < self._interval:
            return False
        if len(self._last) >= MAX_TRACKED_USERS:  # защита от разрастания: забываем устаревшие записи
            self._last = {key: ts for key, ts in self._last.items() if now - ts < self._interval}
        self._last[(bot_id, user_id)] = now
        return True


async def reply_to_disabled_update(
    bot: Bot,
    update: Update,
    throttle: DisabledReplyThrottle,
    owner_telegram_id: Callable[[], Awaitable[int | None]],
) -> bool:
    """Коротко отвечает автору апдейта (сообщение в личке или нажатие кнопки). True — ответ отправлен.

    `owner_telegram_id` вызывается только если ответ действительно будет отправлен (антиспам срабатывает раньше обращения к БД).
    Никогда не выбрасывает исключений: сбой ответа не должен ничего менять в обработке (апдейт всё равно отбрасывается)."""
    try:
        message, callback = update.message, update.callback_query
        if message is not None and message.from_user is not None and message.chat.type == "private":
            user, chat_id, callback_id = message.from_user, message.chat.id, None
        elif callback is not None:
            user, chat_id, callback_id = callback.from_user, None, callback.id
        else:
            return False  # другие виды апдейтов (my_chat_member и т.п.) молча отбрасываются
        if user.is_bot or not await throttle.allow(bot.id, user.id):
            return False
        text = OWNER_TEXT if user.id == await owner_telegram_id() else CLIENT_TEXT
        if callback_id is not None:
            await bot.answer_callback_query(callback_id, text=text, show_alert=True, request_timeout=SEND_TIMEOUT_SECONDS)
        else:
            await bot.send_message(chat_id, text, request_timeout=SEND_TIMEOUT_SECONDS)
        return True
    except Exception as exc:
        # Например, пользователь заблокировал бота (Forbidden) или нет связи с Telegram.
        logger.warning("Не удалось ответить отключённому боту (bot_id=%s): %s", getattr(bot, "id", None), type(exc).__name__)
        return False
