"""Хранилище FSM (этап 7): выбор memory/redis, ключи, TTL, изоляция событий, очистка состояний бота.

Redis используется ТОЛЬКО для FSM (состояния и данные диалогов + короткие блокировки обработки апдейтов одного
пользователя). Схема ключей (aiogram DefaultKeyBuilder, with_bot_id=True):

    bbot:fsm:<bot_id>:<chat_id>[:<thread_id>]:<user_id>:state   — состояние (строка)
    bbot:fsm:<bot_id>:<chat_id>[:<thread_id>]:<user_id>:data    — данные сценария (JSON)
    bbot:fsm:<bot_id>:<chat_id>[:<thread_id>]:<user_id>:lock    — блокировка обработки апдейта (живёт не дольше таймаута)

bot_id в ключе — главный барьер между мастерами: у каждого мастера свой бот, поэтому состояния одного человека в ботах
двух мастеров не пересекаются. state/data живут `fsm_state_ttl_seconds` с момента последней записи.
Строка подключения (REDIS_URL, возможно с паролем) нигде не выводится: в логах и ошибках — только host:port/db.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from app.config.settings import settings

logger = logging.getLogger(__name__)

FsmKind = Literal["memory", "redis"]

FSM_KEY_PREFIX = "bbot:fsm"
LOCK_PART = "lock"

# Redis не должен подвешивать обработку апдейта: быстрый отказ вместо ожидания (Telegram иначе повторяет доставку).
REDIS_CONNECT_TIMEOUT_SECONDS = 3.0
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0
REDIS_HEALTH_CHECK_INTERVAL_SECONDS = 30
REDIS_RETRIES = 1
PING_TIMEOUT_SECONDS = 3.0
# Блокировка «один апдейт одного пользователя за раз»: timeout — сама снимается, если процесс умер, не отпустив её;
# blocking_timeout — сколько ждёт следующий апдейт того же пользователя, прежде чем отказаться (ответ Telegram не зависает).
REDIS_LOCK_KWARGS = {"timeout": 120, "blocking_timeout": 30}

SCAN_COUNT = 500
DELETE_BATCH = 500

# Ошибки, означающие «хранилище FSM недоступно» (а не ошибку хендлера).
STORAGE_ERRORS: tuple[type[BaseException], ...] = (RedisError, ConnectionError, TimeoutError)


class FsmStorageError(RuntimeError):
    """Проблема с хранилищем FSM (конфигурация или недоступность). Текст безопасен для вывода: без пароля и токенов."""


@dataclass
class FsmBackend:
    kind: FsmKind
    storage: BaseStorage
    isolation: BaseEventIsolation


# ---------- выбор хранилища ----------


def resolve_fsm_kind(run_mode: str, fsm_storage: str | None) -> FsmKind:
    """Явный FSM_STORAGE важнее; иначе webhook → redis, polling → memory."""
    if fsm_storage in ("memory", "redis"):
        return fsm_storage  # type: ignore[return-value]
    return "redis" if run_mode == "webhook" else "memory"


def configured_fsm_kind() -> FsmKind:
    return resolve_fsm_kind(settings.run_mode, settings.fsm_storage)


def make_key_builder(prefix: str = FSM_KEY_PREFIX) -> DefaultKeyBuilder:
    """Ключи с bot_id (обязательно): состояния разных ботов/мастеров не пересекаются."""
    return DefaultKeyBuilder(prefix=prefix, with_bot_id=True)


def describe_redis(redis: Redis) -> str:
    """host:port/db подключения — без логина и пароля."""
    kwargs = redis.connection_pool.connection_kwargs
    if kwargs.get("path"):
        return f"unix:{kwargs['path']}"
    return f"{kwargs.get('host', '?')}:{kwargs.get('port', '?')}/{kwargs.get('db', 0)}"


def _make_redis(url: str) -> Redis:
    if not (url or "").strip():
        raise FsmStorageError("REDIS_URL не задан (пример: redis://redis:6379/0)")
    try:
        return Redis.from_url(
            url.strip(),
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            health_check_interval=REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
            # Одна мгновенная повторная попытка: после перезапуска Redis первый запрос попадает на «протухшее» соединение
            # из пула — без повтора он давал бы пользователю лишнюю ошибку. Больше попыток не нужно: при настоящем отказе
            # апдейт должен быстро завершиться (см. FsmStorageGuardMiddleware), а не висеть.
            retry=Retry(NoBackoff(), REDIS_RETRIES),
        )
    except (ValueError, TypeError):
        # Текст исходной ошибки не выводим: в нём может оказаться часть строки подключения.
        raise FsmStorageError("REDIS_URL некорректен (ожидается redis://[:пароль@]хост:порт/номер_бд или rediss://...)") from None


def create_fsm_backend(
    *,
    kind: FsmKind | None = None,
    redis_url: str | None = None,
    state_ttl: int | None = None,
    prefix: str = FSM_KEY_PREFIX,
) -> FsmBackend:
    """Хранилище FSM и изоляция событий по настройкам (параметры нужны тестам). Соединение с Redis ленивое: проверка — ensure_fsm_ready."""
    explicit_memory_in_webhook = kind is None and settings.fsm_storage == "memory" and settings.run_mode == "webhook"
    kind = kind or configured_fsm_kind()
    if kind == "memory":
        if explicit_memory_in_webhook:
            logger.warning(
                "FSM_STORAGE=memory в режиме webhook: диалоги теряются при перезапуске и не общие между процессами. "
                "Для рабочей эксплуатации используйте FSM_STORAGE=redis."
            )
        return FsmBackend("memory", MemoryStorage(), SimpleEventIsolation())

    ttl = settings.fsm_state_ttl_seconds if state_ttl is None else state_ttl
    if ttl <= 0:
        raise FsmStorageError("FSM_STATE_TTL_SECONDS должен быть больше нуля")
    if not prefix:
        raise FsmStorageError("Пустой префикс ключей FSM недопустим")
    redis = _make_redis(settings.redis_url if redis_url is None else redis_url)
    storage = RedisStorage(redis, key_builder=make_key_builder(prefix), state_ttl=ttl, data_ttl=ttl)
    return FsmBackend("redis", storage, storage.create_isolation(lock_kwargs=dict(REDIS_LOCK_KWARGS)))


def isolation_for_storage(storage: BaseStorage) -> BaseEventIsolation:
    """Изоляция событий под уже готовое хранилище (когда хранилище передали в build_dispatcher снаружи)."""
    if isinstance(storage, RedisStorage):
        return storage.create_isolation(lock_kwargs=dict(REDIS_LOCK_KWARGS))
    return SimpleEventIsolation()


def storage_kind(storage: BaseStorage) -> FsmKind:
    return "redis" if isinstance(storage, RedisStorage) else "memory"


def describe_backend(storage: BaseStorage) -> str:
    if isinstance(storage, RedisStorage):
        return (
            f"redis ({describe_redis(storage.redis)}), TTL состояния {storage.state_ttl} с, "
            f"префикс ключей {getattr(storage.key_builder, 'prefix', '?')}"
        )
    return "memory (состояния в памяти процесса)"


# ---------- проверка доступности ----------


async def ping_redis(redis: Redis, timeout: float = PING_TIMEOUT_SECONDS) -> None:
    """Бросает FsmStorageError, если Redis не отвечает за `timeout` секунд (без подробностей подключения)."""
    try:
        await asyncio.wait_for(redis.ping(), timeout)
    except (RedisError, OSError, asyncio.TimeoutError) as exc:
        raise FsmStorageError(f"Redis недоступен ({describe_redis(redis)}): {type(exc).__name__}") from None


async def ensure_fsm_ready(storage: BaseStorage) -> None:
    """Fail fast при старте: для redis — ping; memory всегда готово."""
    if isinstance(storage, RedisStorage):
        await ping_redis(storage.redis)


# ---------- очистка состояний бота ----------


def _glob_escape(text: str) -> str:
    return "".join(f"\\{ch}" if ch in "*?[]\\" else ch for ch in text)


async def clear_bot_states(storage: BaseStorage, bot_id: int) -> int:
    """Удаляет состояния и данные FSM всех пользователей бота `bot_id`. Возвращает число удалённых записей.

    Redis: SCAN по шаблону `<prefix>:<bot_id>:*` (не KEYS: сервер не блокируется), удаление пачками. Ключи блокировок
    (`:lock`) не трогаем: они коротко живут сами, а удаление блокировки под выполняющимся хендлером ломало бы его.
    Memory: записи с этим bot_id.
    """
    bot_id = int(bot_id)
    if isinstance(storage, RedisStorage):
        builder = storage.key_builder
        if not (isinstance(builder, DefaultKeyBuilder) and builder.with_bot_id and builder.prefix):
            raise FsmStorageError("Ключи FSM без bot_id или префикса: очистка по боту невозможна")
        pattern = builder.separator.join([_glob_escape(builder.prefix), str(bot_id), "*"])
        lock_suffix = builder.separator + LOCK_PART
        deleted = 0
        batch: list[Any] = []
        async for key in storage.redis.scan_iter(match=pattern, count=SCAN_COUNT):
            name = key.decode() if isinstance(key, bytes) else key
            if name.endswith(lock_suffix):
                continue
            batch.append(key)
            if len(batch) >= DELETE_BATCH:
                deleted += await storage.redis.unlink(*batch)
                batch = []
        if batch:
            deleted += await storage.redis.unlink(*batch)
        return deleted
    if isinstance(storage, MemoryStorage):
        keys = [key for key in storage.storage if key.bot_id == bot_id]
        for key in keys:
            del storage.storage[key]
        return len(keys)
    raise TypeError(f"Очистка состояний не поддерживается для {type(storage).__name__}")


async def clear_bot_states_from_settings(bot_id: int) -> int | None:
    """Для CLI (отдельный процесс): очищает состояния бота в Redis из настроек.

    None — FSM в памяти процесса бота, из другого процесса её не очистить (состояния сбросятся при перезапуске бота).
    Недоступный Redis → FsmStorageError.
    """
    if configured_fsm_kind() != "redis":
        return None
    backend = create_fsm_backend(kind="redis")
    try:
        await ping_redis(backend.storage.redis)  # type: ignore[attr-defined]
        return await clear_bot_states(backend.storage, bot_id)
    except FsmStorageError:
        raise
    except (RedisError, OSError, asyncio.TimeoutError) as exc:
        raise FsmStorageError(f"Не удалось очистить состояния FSM в Redis: {type(exc).__name__}") from None
    finally:
        await backend.storage.close()


async def close_fsm(dp: Dispatcher) -> None:
    """Закрывает соединения FSM-хранилища (повторный вызов безопасен)."""
    try:
        await dp.fsm.close()
    except Exception:
        logger.exception("Не удалось корректно закрыть хранилище FSM")
