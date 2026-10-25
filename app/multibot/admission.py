"""Допуск апдейтов webhook (этап 12C): ограничение параллелизма до открытия сессии БД.

Зачем (цифры стенда 12B): апдейт держит соединение пула на всё время обработки, включая вызовы Telegram, а пул
ограничен. Без допуска медленный бот или всплеск нагрузки занимают все соединения, остальные мастера ждут `pool_timeout`.
Здесь апдейт сначала получает «место» — сначала в очереди своего бота, затем в общей, — и только потом открывает сессию.
Если место не освободилось за `timeout`, поднимается AdmissionTimeout: webhook отвечает 503, Telegram повторит доставку.

Состояние — в памяти процесса (рабочая схема — один экземпляр приложения).
"""

import asyncio
import time
from contextlib import asynccontextmanager


class AdmissionTimeout(Exception):
    """Не дождались свободного места для обработки апдейта."""


class UpdateAdmission:
    def __init__(self, *, max_total: int, max_per_bot: int, timeout_seconds: float) -> None:
        if max_total < 1 or max_per_bot < 1:
            raise ValueError("Лимиты параллелизма должны быть не меньше 1")
        self._total = asyncio.Semaphore(max_total)
        self._max_per_bot = min(max_per_bot, max_total)
        self._per_bot: dict[int, asyncio.Semaphore] = {}
        self._timeout = timeout_seconds

    def _bot_semaphore(self, bot_id: int) -> asyncio.Semaphore:
        semaphore = self._per_bot.get(bot_id)
        if semaphore is None:
            semaphore = self._per_bot[bot_id] = asyncio.Semaphore(self._max_per_bot)
        return semaphore

    @asynccontextmanager
    async def slot(self, bot_id: int):
        deadline = time.monotonic() + self._timeout
        bot_semaphore = self._bot_semaphore(bot_id)
        # Сначала место своего бота: застрявшие апдейты одного бота не должны занимать общие места.
        await self._acquire(bot_semaphore, deadline)
        try:
            await self._acquire(self._total, deadline)
        except BaseException:
            bot_semaphore.release()
            raise
        try:
            yield
        finally:
            self._total.release()
            bot_semaphore.release()

    @staticmethod
    async def _acquire(semaphore: asyncio.Semaphore, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=max(remaining, 0.0))
        except asyncio.TimeoutError:
            raise AdmissionTimeout from None
