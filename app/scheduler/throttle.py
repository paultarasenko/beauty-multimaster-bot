"""Ограничение скорости отправки на бота (этап 6).

У каждого мастера свой бот, а лимит Telegram (около 30 сообщений в секунду) считается на бота, поэтому ограничитель
не глобальный, а свой на каждый бот: один «шумный» мастер не замедляет остальных. Ограничитель равномерно разносит
отправки во времени (без всплесков): не чаще одной в `1 / rate` секунд.

Здесь же — пауза бота после `TelegramRetryAfter`/сетевой ошибки: пока она не истекла, планировщик не пытается слать
этим ботом (и не превращает ошибку в шторм повторов).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if rate_per_second <= 0:
            raise ValueError("rate_per_second должен быть > 0")
        self.interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Ждёт своей очереди; вызовы строго по очереди, слоты — через `interval` друг от друга."""
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next_slot = max(now, self._next_slot) + self.interval


class BotThrottle:
    """Состояние одного бота: ограничитель скорости и «пауза до» после RetryAfter/сетевой ошибки."""

    def __init__(self, rate_per_second: float, *, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self.limiter = RateLimiter(rate_per_second, clock=clock, sleep=sleep)
        self._clock = clock
        self._paused_until = 0.0

    def pause(self, seconds: float) -> None:
        self._paused_until = max(self._paused_until, self._clock() + max(seconds, 0.0))

    def remaining_pause(self) -> float:
        return max(0.0, self._paused_until - self._clock())

    @property
    def is_paused(self) -> bool:
        return self.remaining_pause() > 0


class BotThrottles:
    """Состояние по мастерам (у мастера один бот). Ключ — master_id; смена токена бота состояния не сбрасывает."""

    def __init__(self, rate_per_second: float, *, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self._rate = rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._by_master: dict[int, BotThrottle] = {}

    def for_master(self, master_id: int) -> BotThrottle:
        throttle = self._by_master.get(master_id)
        if throttle is None:
            throttle = self._by_master[master_id] = BotThrottle(self._rate, clock=self._clock, sleep=self._sleep)
        return throttle

    def forget_except(self, master_ids: set[int]) -> None:
        """Забывает мастеров, которых больше нет среди обслуживаемых (бот отключён/удалён) — словарь не растёт."""
        for master_id in set(self._by_master) - master_ids:
            del self._by_master[master_id]
