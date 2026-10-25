"""Охранник вызовов Bot API (этап 12C): единая точка для 429 и недействительного токена на ВСЕХ путях отправки.

Подключается к общей HTTP-сессии (`telegram_session()`), поэтому видит и ответы на апдейты в хендлерах, и рассылку
планировщика:
- 429 (flood control): запоминаем «бот на паузе до …» и пока пауза идёт, НЕ ходим в Telegram, а сразу поднимаем
  `TelegramRetryAfter` с остатком паузы — так retry_after учитывается и хендлерами, и планировщиком, а запросы не
  сыплются в уже ограничивающий бот;
- 401 (недействительный токен): один раз за интервал вызываем `on_unauthorized(bot_id)` (в runner это «пометить бота
  неактивным + один сигнал суперадмину»), остальные вызовы с тем же ботом только пробрасывают ошибку без новых сигналов.

Состояние — в памяти процесса (рабочая схема — один экземпляр приложения, см. ARCHITECTURE.md, «Этап 12C»).
"""

import logging
import math
import time
from collections.abc import Awaitable, Callable

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramUnauthorizedError

logger = logging.getLogger(__name__)

MAX_PAUSE_SECONDS = 3600.0  # защита от «мусорного» retry_after: дольше часа не блокируем
DEFAULT_REPORT_INTERVAL_SECONDS = 3600.0

UnauthorizedCallback = Callable[[int], Awaitable[None]]


class TelegramCallGuard(BaseRequestMiddleware):
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        report_interval_seconds: float = DEFAULT_REPORT_INTERVAL_SECONDS,
    ) -> None:
        self._clock = clock
        self._report_interval = report_interval_seconds
        self._paused_until: dict[int, float] = {}
        self._reported_at: dict[int, float] = {}
        self.on_unauthorized: UnauthorizedCallback | None = None

    def paused_for(self, bot_id: int) -> float:
        """Сколько секунд бот ещё на паузе из-за 429 (0 — не на паузе)."""
        until = self._paused_until.get(bot_id)
        if until is None:
            return 0.0
        remaining = until - self._clock()
        if remaining <= 0:
            self._paused_until.pop(bot_id, None)
            return 0.0
        return remaining

    async def __call__(self, make_request, bot, method):
        bot_id = bot.id
        remaining = self.paused_for(bot_id)
        if remaining > 0:
            raise TelegramRetryAfter(method=method, message="bot is paused after flood control", retry_after=math.ceil(remaining))
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as exc:
            delay = min(max(float(exc.retry_after), 0.0), MAX_PAUSE_SECONDS)
            self._paused_until[bot_id] = self._clock() + delay
            logger.warning("Telegram просит подождать %.0f с (bot_id=%s): бот на паузе", delay, bot_id)
            raise
        except TelegramUnauthorizedError:
            await self._report_unauthorized(bot_id)
            raise

    async def _report_unauthorized(self, bot_id: int) -> None:
        now = self._clock()
        last = self._reported_at.get(bot_id)
        if last is not None and now - last < self._report_interval:
            return  # сигнал по этому боту уже был — серии не будет
        self._reported_at[bot_id] = now
        callback = self.on_unauthorized
        if callback is None:
            return
        try:
            await callback(bot_id)
        except Exception:
            logger.exception("Не удалось обработать недействительный токен бота %s", bot_id)


# Один охранник на процесс: его подключает telegram_session(), а runner задаёт on_unauthorized.
call_guard = TelegramCallGuard()
