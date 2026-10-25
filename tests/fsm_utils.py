"""Хранилище FSM в тестах (этап 7): memory или настоящий Redis, переключается параметром `fsm_kind` (см. conftest.py).

Redis берётся из docker-compose.test.yml (порт 6380) или из TEST_REDIS_URL. Каждый тест работает под своим уникальным
префиксом ключей и после себя удаляет только их — общий Redis (и другие тесты) не затрагивается, FLUSHDB не вызывается.
"""

import os
import uuid

import redis as sync_redis

from app.bot.fsm_storage import FsmBackend, create_fsm_backend

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/0")
SKIP_REASON = (
    f"Redis недоступен ({TEST_REDIS_URL.rsplit('@', 1)[-1]}): запустите `docker compose -f docker-compose.test.yml up -d --wait` "
    "или локальный redis-server и задайте TEST_REDIS_URL"
)


def redis_is_available() -> bool:
    client = sync_redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    try:
        return bool(client.ping())
    except sync_redis.RedisError:
        return False
    finally:
        client.close()


class ActiveFsm:
    """Какое хранилище используют стенды (`tests/tg.py`, e2e-фикстуры) в текущем тесте. Выставляет фикстура `fsm_kind`."""

    kind = "memory"
    prefix = "bbot:test"


def make_backend() -> FsmBackend:
    """Новое хранилище FSM + изоляция событий под текущий `fsm_kind`; для redis закрывать через `await backend.storage.close()`."""
    if ActiveFsm.kind == "redis":
        return create_fsm_backend(kind="redis", redis_url=TEST_REDIS_URL, state_ttl=600, prefix=ActiveFsm.prefix)
    return create_fsm_backend(kind="memory")


def new_prefix() -> str:
    return f"bbot:test:{uuid.uuid4().hex[:12]}"


def purge_prefix(prefix: str) -> int:
    """Удаляет только ключи этого префикса (SCAN, без KEYS/FLUSHDB)."""
    client = sync_redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1, socket_timeout=2)
    try:
        keys = list(client.scan_iter(match=f"{prefix}:*", count=500))
        return client.delete(*keys) if keys else 0
    finally:
        client.close()


def sync_keys(prefix: str) -> list[str]:
    client = sync_redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1, socket_timeout=2)
    try:
        return sorted(k.decode() for k in client.scan_iter(match=f"{prefix}:*", count=500))
    finally:
        client.close()
