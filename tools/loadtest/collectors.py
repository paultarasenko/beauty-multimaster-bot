"""Замеры инфраструктуры во время прогона: соединения PostgreSQL, память Redis, CPU/память контейнеров (если доступно)."""

import asyncio
import logging
import re
from dataclasses import dataclass, field

import asyncpg

logger = logging.getLogger("loadtest")

PG_QUERY = """
SELECT count(*) FILTER (WHERE pid <> pg_backend_pid()) AS total,
       count(*) FILTER (WHERE state = 'active' AND pid <> pg_backend_pid()) AS active,
       count(*) FILTER (WHERE state LIKE 'idle in transaction%') AS idle_in_tx,
       coalesce(max(extract(epoch FROM now() - xact_start)) FILTER (WHERE state LIKE 'idle in transaction%'), 0) AS max_idle_tx_s
FROM pg_stat_activity WHERE datname = current_database() AND backend_type = 'client backend'
"""


@dataclass
class Sampler:
    """Фоновый опрос раз в `interval` секунд; значения — в `rows` как (секунды от начала прогона, имя, значение)."""

    interval: float
    origin: float
    rows: list[tuple[float, str, float]] = field(default_factory=list)
    _task: asyncio.Task | None = None

    async def _poll(self) -> None:
        raise NotImplementedError

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await self._poll()
            except Exception as exc:  # замер не должен ронять прогон
                logger.warning("замер %s не удался: %s", type(self).__name__, type(exc).__name__)
            await asyncio.sleep(self.interval)

    def _add(self, name: str, value: float) -> None:
        self.rows.append((round(asyncio.get_running_loop().time() - self.origin, 2), name, float(value)))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


class PgSampler(Sampler):
    """pg_stat_activity: сколько соединений у базы, сколько из них работает и сколько «простаивает внутри транзакции»."""

    def __init__(self, dsn: str, origin: float, interval: float = 1.0):
        super().__init__(interval, origin)
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None
        self.max_connections: int | None = None

    async def start(self) -> None:
        self._conn = await asyncpg.connect(self._dsn)
        self.max_connections = int(await self._conn.fetchval("SHOW max_connections"))
        await super().start()

    async def _poll(self) -> None:
        row = await self._conn.fetchrow(PG_QUERY)
        for name in ("total", "active", "idle_in_tx", "max_idle_tx_s"):
            self._add(f"pg_{name}", row[name])

    async def stop(self) -> None:
        await super().stop()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class RedisSampler(Sampler):
    def __init__(self, url: str, origin: float, interval: float = 2.0):
        super().__init__(interval, origin)
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url)

    async def _poll(self) -> None:
        info = await self._redis.info("memory")
        self._add("redis_used_mb", info["used_memory"] / 1024 / 1024)
        self._add("redis_keys", await self._redis.dbsize())

    async def stop(self) -> None:
        await super().stop()
        await self._redis.aclose()


def series(rows: list[tuple[float, str, float]], name: str) -> list[float]:
    return [value for _, n, value in rows if n == name]


# ---- docker stats ----

_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024**2, "gib": 1024**3}


def _to_mib(text: str) -> float | None:
    match = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text)
    if not match or match.group(2).lower() not in _UNITS:
        return None
    return float(match.group(1)) * _UNITS[match.group(2).lower()] / 1024**2


def parse_docker_stats(text: str, since: float | None = None, until: float | None = None) -> dict[str, dict]:
    """Строки `unix_time,имя_контейнера,CPU%,память_использовано / лимит` (их пишет run.sh). Итог по контейнеру: средний и
    наибольший CPU (в % одного ядра), наибольшая память в МиБ, число замеров. Строки вне [since, until] пропускаются."""
    result: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.strip().split(",", 3)
        if len(parts) != 4:
            continue
        try:
            stamp = float(parts[0])
            cpu = float(parts[2].rstrip("%"))
        except ValueError:
            continue
        mem = _to_mib(parts[3].split("/")[0])
        if (since is not None and stamp < since) or (until is not None and stamp > until) or mem is None:
            continue
        entry = result.setdefault(parts[1], {"cpu": [], "mem_mib": []})
        entry["cpu"].append(cpu)
        entry["mem_mib"].append(mem)
    return {
        name: {"cpu_avg": sum(v["cpu"]) / len(v["cpu"]), "cpu_max": max(v["cpu"]), "mem_max_mib": max(v["mem_mib"]), "samples": len(v["cpu"])}
        for name, v in result.items()
    }
