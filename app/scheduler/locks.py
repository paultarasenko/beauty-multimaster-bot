"""Защита периодических задач от двойного запуска (этап 12C): PostgreSQL advisory lock вокруг тика.

Сценарий: при обновлении контейнеров старый и новый экземпляр на какое-то время работают одновременно, и оба запускают
тики планировщика. Построчная защита (FOR UPDATE SKIP LOCKED, claim) дубликатов уведомлений не допускает и так, а замок
убирает лишнюю работу и повторные сводки: пока тик выполняется в одном процессе, тот же тик в другом ПРОПУСКАЕТСЯ.

Устройство: замок сессионный (`pg_try_advisory_lock`) и держится на ОТДЕЛЬНОМ соединении в режиме AUTOCOMMIT — оно не
участвует в транзакциях тика и не висит «idle in transaction». Если процесс умер, соединение закрывается, и PostgreSQL
снимает замок сам. Если снять замок штатно не удалось, соединение закрывается принудительно (invalidate), а не
возвращается в пул с повисшим замком. Без лидер-выборов и общего реестра: только «замок занят — пропускаем тик».
Дополнительно тик занимает одно соединение пула на всё время выполнения (учтено в расчёте пула в README).
"""

import logging
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

LOCK_NAMESPACE = 0x62626F74  # «bbot»: первое число пары ключей advisory lock; свободно от замков других приложений на этой БД


def lock_key(name: str) -> int:
    """Стабильное неотрицательное int4-число для имени тика (второе число пары ключей)."""
    return zlib.crc32(f"tick:{name}".encode()) & 0x7FFFFFFF


@asynccontextmanager
async def tick_lock(session_factory: async_sessionmaker, name: str) -> AsyncIterator[bool]:
    """`async with tick_lock(factory, "notifications") as acquired:` — True, если замок взят и тик можно выполнять.

    False — замок занят другим процессом (или БД недоступна): тик пропускается, на следующем тике попробуем снова.
    """
    engine = session_factory.kw["bind"]
    key = lock_key(name)
    conn = None
    acquired = False
    try:
        conn = await engine.connect()
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        result = await conn.execute(text("SELECT pg_try_advisory_lock(:ns, :key)"), {"ns": LOCK_NAMESPACE, "key": key})
        acquired = bool(result.scalar())
    except Exception:
        logger.exception("Планировщик (%s): не удалось взять advisory lock, тик пропущен", name)
        acquired = False
    if not acquired:
        await _close(conn, invalidate=False)
        yield False
        return
    released = False
    try:
        yield True
    finally:
        try:
            result = await conn.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), {"ns": LOCK_NAMESPACE, "key": key})
            released = bool(result.scalar())
        except BaseException:
            released = False
            raise
        finally:
            # Не удалось подтвердить снятие замка — соединение уничтожаем: закрытие сессии PostgreSQL снимает замок.
            await _close(conn, invalidate=not released)


async def _close(conn, *, invalidate: bool) -> None:
    if conn is None:
        return
    try:
        if invalidate:
            await conn.invalidate()
        await conn.close()
    except Exception:
        logger.exception("Не удалось корректно закрыть соединение advisory lock")
