"""Этап 7: хранилище FSM — выбор по режиму и настройкам, ключи, TTL, переживание перезапуска, изоляция ботов,
очистка состояний бота, изоляция событий между процессами. Redis — настоящий (docker-compose.test.yml, порт 6380);
без Redis эти тесты пропускаются с понятной причиной (см. tests/fsm_utils.py).
"""

import asyncio
import datetime as dt
import logging

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.types import Chat, Message, Update, User
from pydantic import ValidationError

from app.bot import fsm_storage
from app.bot.dispatcher import build_dispatcher
from app.bot.fsm_storage import (
    FSM_KEY_PREFIX,
    FsmStorageError,
    clear_bot_states,
    create_fsm_backend,
    describe_redis,
    resolve_fsm_kind,
)
from app.bot.states import booking_states
from app.config.settings import Settings, settings
from tests import fsm_utils
from tests.tg import RecordingSession

BOT_A, BOT_B = 1001, 1002


def key(bot_id: int = BOT_A, user_id: int = 5001, chat_id: int | None = None) -> StorageKey:
    return StorageKey(bot_id=bot_id, chat_id=user_id if chat_id is None else chat_id, user_id=user_id)


def redis_backend(url: str, prefix: str, *, ttl: int = 600):
    return create_fsm_backend(kind="redis", redis_url=url, state_ttl=ttl, prefix=prefix)


# ---------- выбор хранилища ----------


@pytest.mark.parametrize(
    ("run_mode", "fsm_storage_setting", "expected"),
    [
        ("webhook", None, "redis"),
        ("polling", None, "memory"),
        ("webhook", "memory", "memory"),
        ("webhook", "redis", "redis"),
        ("polling", "redis", "redis"),
        ("polling", "memory", "memory"),
    ],
)
def test_storage_kind_by_mode_and_setting(run_mode, fsm_storage_setting, expected):
    assert resolve_fsm_kind(run_mode, fsm_storage_setting) == expected


def test_settings_defaults_and_validation():
    defaults = Settings(_env_file=None)
    assert defaults.fsm_storage is None  # «по режиму»
    assert defaults.fsm_state_ttl_seconds == 3 * 24 * 3600
    assert Settings(_env_file=None, fsm_storage="").fsm_storage is None  # пустой FSM_STORAGE в .env — тоже «по режиму»
    assert Settings(_env_file=None, fsm_storage=" Redis ").fsm_storage == "redis"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, fsm_storage="sqlite")
    for bad_ttl in (0, -5):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, fsm_state_ttl_seconds=bad_ttl)


def test_build_dispatcher_selects_storage_by_run_mode(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6380/0")
    monkeypatch.setattr(settings, "fsm_storage", None)

    monkeypatch.setattr(settings, "run_mode", "polling")
    dp = build_dispatcher(None, routers=[])
    assert isinstance(dp.storage, MemoryStorage) and isinstance(dp.fsm.events_isolation, SimpleEventIsolation)

    monkeypatch.setattr(settings, "run_mode", "webhook")
    dp = build_dispatcher(None, routers=[])
    assert isinstance(dp.storage, RedisStorage) and isinstance(dp.fsm.events_isolation, RedisEventIsolation)


def test_redis_storage_is_configured_with_bot_id_prefix_ttl_and_lock_timeouts(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "webhook")
    monkeypatch.setattr(settings, "fsm_storage", None)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6380/3")
    monkeypatch.setattr(settings, "fsm_state_ttl_seconds", 4321)
    storage = build_dispatcher(None, routers=[]).storage
    builder = storage.key_builder
    assert builder.with_bot_id is True and builder.prefix == FSM_KEY_PREFIX
    assert storage.state_ttl == 4321 and storage.data_ttl == 4321
    isolation = build_dispatcher(None, routers=[]).fsm.events_isolation
    # блокировка не вечная: умершая реплика не заблокирует пользователя навсегда, а ожидание следующего апдейта ограничено
    assert isolation.lock_kwargs["timeout"] > 0 and isolation.lock_kwargs["blocking_timeout"] > 0
    assert storage.redis.connection_pool.connection_kwargs["db"] == 3


def test_explicit_memory_in_webhook_mode_is_allowed_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(settings, "run_mode", "webhook")
    monkeypatch.setattr(settings, "fsm_storage", "memory")
    with caplog.at_level(logging.WARNING, logger="app.bot.fsm_storage"):
        dp = build_dispatcher(None, routers=[])
    assert isinstance(dp.storage, MemoryStorage)
    assert any("FSM_STORAGE=memory" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_default_polling_memory_has_no_warning(monkeypatch, caplog):
    monkeypatch.setattr(settings, "run_mode", "polling")
    monkeypatch.setattr(settings, "fsm_storage", None)
    with caplog.at_level(logging.WARNING, logger="app.bot.fsm_storage"):
        build_dispatcher(None, routers=[])
    assert not [r for r in caplog.records if r.name == "app.bot.fsm_storage"]


def test_explicit_storage_argument_wins_and_gets_matching_isolation(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "webhook")  # по настройкам был бы redis
    memory = MemoryStorage()
    dp = build_dispatcher(None, storage=memory, routers=[])
    assert dp.storage is memory and isinstance(dp.fsm.events_isolation, SimpleEventIsolation)


@pytest.mark.parametrize("bad_url", ["", "   ", "not-a-redis-url", "http://localhost:6379/0"])
def test_invalid_redis_url_is_a_clear_error_without_the_url(bad_url):
    with pytest.raises(FsmStorageError) as exc:
        create_fsm_backend(kind="redis", redis_url=bad_url)
    assert bad_url.strip() == "" or bad_url not in str(exc.value)


def test_non_positive_ttl_is_rejected():
    with pytest.raises(FsmStorageError):
        create_fsm_backend(kind="redis", redis_url="redis://localhost:6380/0", state_ttl=0)


def test_redis_password_never_appears_in_descriptions_or_errors():
    backend = create_fsm_backend(kind="redis", redis_url="redis://user:TopSecretPw@localhost:6390/2", prefix="x:fsm")
    description = fsm_storage.describe_backend(backend.storage)
    assert "TopSecretPw" not in description and "localhost:6390/2" in description
    assert "TopSecretPw" not in describe_redis(backend.storage.redis)

    async def ping() -> str:
        try:
            await fsm_storage.ensure_fsm_ready(backend.storage)
        except FsmStorageError as exc:
            return str(exc)
        finally:
            await backend.storage.close()
        return ""

    message = asyncio.run(ping())
    assert message and "TopSecretPw" not in message and "localhost:6390" in message


# ---------- Redis: ключи, TTL, данные ----------


async def test_keys_contain_bot_id_and_follow_the_documented_scheme(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix)
    try:
        await backend.storage.set_state(key(BOT_A, 5001), "BookingFSM:select_service")
        await backend.storage.set_data(key(BOT_A, 5001), {"service_id": 7})
        assert fsm_utils.sync_keys(fsm_prefix) == [
            f"{fsm_prefix}:{BOT_A}:5001:5001:data",
            f"{fsm_prefix}:{BOT_A}:5001:5001:state",
        ]
    finally:
        await backend.storage.close()


async def test_state_and_data_get_ttl_and_every_write_refreshes_it(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix, ttl=500)
    storage = backend.storage
    try:
        await storage.set_state(key(), "S:x")
        await storage.set_data(key(), {"a": 1})
        ttls = [await storage.redis.ttl(f"{fsm_prefix}:{BOT_A}:5001:5001:{part}") for part in ("state", "data")]
        assert all(0 < t <= 500 for t in ttls), ttls  # без TTL было бы -1

        await storage.redis.expire(f"{fsm_prefix}:{BOT_A}:5001:5001:data", 5)
        await FSMContext(storage, key()).update_data(b=2)  # обычная запись хендлера продлевает жизнь данных
        assert await storage.redis.ttl(f"{fsm_prefix}:{BOT_A}:5001:5001:data") > 400
    finally:
        await storage.close()


async def test_ttl_expiry_drops_state_and_data(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix, ttl=1)
    try:
        await backend.storage.set_state(key(), "S:x")
        await backend.storage.set_data(key(), {"a": 1})
        await asyncio.sleep(1.6)
        assert await backend.storage.get_state(key()) is None and await backend.storage.get_data(key()) == {}
    finally:
        await backend.storage.close()


JSON_SAMPLE = {
    "service_id": 12,
    "dates_page": 0,
    "selected_time": dt.datetime(2030, 5, 17, 14, 30, tzinfo=dt.timezone(dt.timedelta(hours=2))).isoformat(),
    "block_date": dt.date(2030, 5, 17).isoformat(),
    "start_time": dt.time(9, 30).isoformat(),
    "price": "35.50",
    "title": None,
    "review_edit": False,
    "client_name": "Анна «Тест» 💅",
}


async def test_data_round_trips_through_redis_unchanged_and_parses_back(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix)
    try:
        await backend.storage.set_data(key(), dict(JSON_SAMPLE))
        data = await backend.storage.get_data(key())
    finally:
        await backend.storage.close()
    assert data == JSON_SAMPLE
    # то, как хендлеры разбирают сохранённое (ISO → объекты)
    assert dt.datetime.fromisoformat(data["selected_time"]).utcoffset() == dt.timedelta(hours=2)
    assert dt.date.fromisoformat(data["block_date"]) == dt.date(2030, 5, 17)
    assert dt.time.fromisoformat(data["start_time"]) == dt.time(9, 30)


async def test_non_json_values_would_fail_loudly_not_silently(redis_url, fsm_prefix):
    """Страховка аудита: Redis не принимает date/Decimal — поэтому хендлеры кладут ISO-строки (см. отчёт этапа 7)."""
    backend = redis_backend(redis_url, fsm_prefix)
    try:
        with pytest.raises(TypeError):
            await backend.storage.set_data(key(), {"day": dt.date(2030, 1, 1)})
    finally:
        await backend.storage.close()


def _all_states() -> list:
    groups = [g for g in vars(booking_states).values() if isinstance(g, type) and issubclass(g, StatesGroup) and g is not StatesGroup]
    return [(g.__name__, state) for g in groups for state in g.__states__]


def test_every_fsm_group_is_discovered():
    names = {name for name, _ in _all_states()}
    assert len(names) == 16 and {"BookingFSM", "AdminAboutFSM", "MasterReplyFSM", "ContactMasterFSM", "AdminSettingsFSM", "AdminSpecialDayFSM"} <= names  # 15: + AdminSettingsFSM (этап 8); 16: + AdminSpecialDayFSM (особые дни)


@pytest.mark.parametrize(("group", "state"), _all_states(), ids=lambda v: v if isinstance(v, str) else v.state)
async def test_every_fsm_state_round_trips_through_redis_and_survives_restart(redis_url, fsm_prefix, group, state):
    first = redis_backend(redis_url, fsm_prefix)
    ctx = FSMContext(first.storage, key())
    await ctx.set_state(state)
    await ctx.update_data(**JSON_SAMPLE)
    await first.storage.close()  # «перезапуск процесса»: новое соединение, новый объект хранилища

    second = redis_backend(redis_url, fsm_prefix)
    try:
        restored = FSMContext(second.storage, key())
        assert await restored.get_state() == state.state
        assert await restored.get_data() == JSON_SAMPLE
        await restored.clear()
        assert await restored.get_state() is None and await restored.get_data() == {}
    finally:
        await second.storage.close()


async def test_states_survive_dispatcher_recreation(redis_url, fsm_prefix):
    """Состояние, записанное одним Dispatcher, видит другой, собранный заново (деплой/перезапуск)."""
    first = redis_backend(redis_url, fsm_prefix)
    dp1 = Dispatcher(storage=first.storage, events_isolation=first.isolation)
    await FSMContext(dp1.storage, key()).set_state(booking_states.BookingFSM.select_date)
    await dp1.fsm.close()

    second = redis_backend(redis_url, fsm_prefix)
    dp2 = Dispatcher(storage=second.storage, events_isolation=second.isolation)
    try:
        assert await FSMContext(dp2.storage, key()).get_state() == booking_states.BookingFSM.select_date.state
    finally:
        await dp2.fsm.close()


async def test_one_person_in_two_masters_bots_has_independent_states(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix)
    try:
        a, b = FSMContext(backend.storage, key(BOT_A, 777)), FSMContext(backend.storage, key(BOT_B, 777))
        await a.set_state(booking_states.BookingFSM.select_date)
        await a.update_data(service_id=1)
        assert await b.get_state() is None and await b.get_data() == {}
        await b.set_state(booking_states.ReviewFSM.enter_name)
        await b.update_data(service_id=2)
        assert (await a.get_state(), await a.get_data()) == (booking_states.BookingFSM.select_date.state, {"service_id": 1})
        await a.clear()
        assert await b.get_state() == booking_states.ReviewFSM.enter_name.state and await b.get_data() == {"service_id": 2}
    finally:
        await backend.storage.close()


async def test_parallel_scenarios_of_many_users_in_two_bots_do_not_mix(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix)
    storage = backend.storage

    async def scenario(bot_id: int, user_id: int) -> None:
        ctx = FSMContext(storage, key(bot_id, user_id))
        for step in range(5):
            await ctx.update_data(**{f"step{step}": f"{bot_id}:{user_id}:{step}"})
            await asyncio.sleep(0)
        await ctx.set_state(booking_states.BookingFSM.select_date)

    try:
        pairs = [(bot, user) for bot in (BOT_A, BOT_B) for user in range(100, 125)]
        await asyncio.gather(*(scenario(bot, user) for bot, user in pairs))
        for bot, user in pairs:
            data = await storage.get_data(key(bot, user))
            assert data == {f"step{i}": f"{bot}:{user}:{i}" for i in range(5)}
    finally:
        await storage.close()


# ---------- очистка состояний бота ----------


async def _seed(storage, bot_id: int, users=(1, 2, 3)) -> None:
    for user in users:
        await storage.set_state(key(bot_id, user), "S:x")
        await storage.set_data(key(bot_id, user), {"u": user})


async def test_clear_bot_states_removes_only_that_bots_keys(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix)
    storage = backend.storage
    try:
        # «соседние» id: шаблон не должен задеть бота, чей id начинается с тех же цифр
        for bot in (12, 123, 1234):
            await _seed(storage, bot)
        before = set(fsm_utils.sync_keys(fsm_prefix))
        assert await clear_bot_states(storage, 123) == 6  # 3 пользователя × (state + data)
        after = set(fsm_utils.sync_keys(fsm_prefix))
        assert before - after == {f"{fsm_prefix}:123:{u}:{u}:{p}" for u in (1, 2, 3) for p in ("state", "data")}
        assert await storage.get_state(key(12, 1)) == "S:x" and await storage.get_data(key(1234, 3)) == {"u": 3}
        assert await storage.get_state(key(123, 1)) is None
        assert await clear_bot_states(storage, 123) == 0  # повторный вызов безопасен
    finally:
        await storage.close()


async def test_clear_bot_states_uses_scan_not_keys_and_handles_many_keys(redis_url, fsm_prefix, monkeypatch):
    backend = redis_backend(redis_url, fsm_prefix)
    storage = backend.storage

    async def forbidden_keys(*args, **kwargs):
        raise AssertionError("KEYS блокирует Redis — очистка обязана идти через SCAN")

    monkeypatch.setattr(storage.redis, "keys", forbidden_keys)
    monkeypatch.setattr(fsm_storage, "DELETE_BATCH", 7)  # несколько пачек удаления
    monkeypatch.setattr(fsm_storage, "SCAN_COUNT", 5)  # несколько итераций SCAN
    scans = []
    original_scan = storage.redis.scan

    async def counting_scan(*args, **kwargs):
        scans.append(kwargs.get("match"))
        return await original_scan(*args, **kwargs)

    monkeypatch.setattr(storage.redis, "scan", counting_scan)
    try:
        await _seed(storage, BOT_A, users=range(1, 41))
        await _seed(storage, BOT_B, users=range(1, 6))
        assert await clear_bot_states(storage, BOT_A) == 80
        assert len(scans) > 1 and set(scans) == {f"{fsm_prefix}:{BOT_A}:*"}
        assert len(fsm_utils.sync_keys(fsm_prefix)) == 10  # бот B не тронут
    finally:
        await storage.close()


async def test_clear_bot_states_leaves_lock_keys_alone(redis_url, fsm_prefix):
    backend = redis_backend(redis_url, fsm_prefix)
    storage = backend.storage
    try:
        await _seed(storage, BOT_A, users=(1,))
        async with backend.isolation.lock(key(BOT_A, 1)):
            # хендлер этого пользователя сейчас выполняется: очистка не должна выбить у него блокировку из-под ног
            assert await clear_bot_states(storage, BOT_A) == 2
            assert f"{fsm_prefix}:{BOT_A}:1:1:lock" in fsm_utils.sync_keys(fsm_prefix)
        assert fsm_utils.sync_keys(fsm_prefix) == []  # после выхода блокировка снята штатно, без LockNotOwnedError
    finally:
        await storage.close()


async def test_clear_bot_states_refuses_keys_without_bot_id(redis_url, fsm_prefix):
    """Ключи без bot_id очистить «по боту» нельзя — лучше отказ, чем удаление чужого."""
    from aiogram.fsm.storage.base import DefaultKeyBuilder

    storage = RedisStorage(
        create_fsm_backend(kind="redis", redis_url=redis_url, prefix=fsm_prefix).storage.redis,
        key_builder=DefaultKeyBuilder(prefix=fsm_prefix, with_bot_id=False),
    )
    try:
        with pytest.raises(FsmStorageError):
            await clear_bot_states(storage, BOT_A)
    finally:
        await storage.close()


async def test_clear_bot_states_escapes_glob_characters_of_prefix(redis_url):
    prefix = fsm_utils.new_prefix() + ":a*b"  # `*` в префиксе не должен превращаться в маску
    other = prefix.replace("a*b", "aXXb")
    first, second = redis_backend(redis_url, prefix), redis_backend(redis_url, other)
    try:
        await _seed(first.storage, BOT_A, users=(1,))
        await _seed(second.storage, BOT_A, users=(1,))
        assert await clear_bot_states(first.storage, BOT_A) == 2
        assert await second.storage.get_state(key(BOT_A, 1)) == "S:x"
    finally:
        await first.storage.close()
        await second.storage.close()
        fsm_utils.purge_prefix(other)
        fsm_utils.purge_prefix(prefix)


async def test_clear_bot_states_memory_analogue():
    storage = MemoryStorage()
    await _seed(storage, BOT_A)
    await _seed(storage, BOT_B)
    assert await clear_bot_states(storage, BOT_A) == 3
    # (читать удалённый ключ через get_state нельзя: defaultdict в MemoryStorage создаёт пустую запись при обращении)
    assert not [k for k in storage.storage if k.bot_id == BOT_A] and len([k for k in storage.storage if k.bot_id == BOT_B]) == 3
    assert await clear_bot_states(storage, BOT_A) == 0
    assert await storage.get_state(key(BOT_B, 1)) == "S:x"


async def test_clear_bot_states_rejects_unknown_storage_type():
    from aiogram.fsm.storage.base import BaseStorage

    class Other(BaseStorage):
        async def set_state(self, key, state=None): ...
        async def get_state(self, key): ...
        async def set_data(self, key, data): ...
        async def get_data(self, key): ...
        async def close(self): ...

    with pytest.raises(TypeError):
        await clear_bot_states(Other(), BOT_A)


async def test_clear_from_settings_cleans_redis_and_closes_connection(redis_url, fsm_prefix, monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "webhook")
    monkeypatch.setattr(settings, "fsm_storage", None)
    monkeypatch.setattr(settings, "redis_url", redis_url)
    seed = redis_backend(redis_url, FSM_KEY_PREFIX)  # реальный префикс приложения; ключи с «тестовым» bot_id вычищаем в finally
    bot_id = 987_654_321
    try:
        await _seed(seed.storage, bot_id, users=(1, 2))
        assert await fsm_storage.clear_bot_states_from_settings(bot_id) == 4
        assert await seed.storage.get_state(key(bot_id, 1)) is None
    finally:
        await clear_bot_states(seed.storage, bot_id)
        await seed.storage.close()


async def test_clear_from_settings_memory_mode_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "polling")
    monkeypatch.setattr(settings, "fsm_storage", None)
    assert await fsm_storage.clear_bot_states_from_settings(BOT_A) is None


async def test_clear_from_settings_unreachable_redis_is_a_storage_error(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "webhook")
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6399/0")
    with pytest.raises(FsmStorageError):
        await fsm_storage.clear_bot_states_from_settings(BOT_A)


# ---------- изоляция событий между «процессами» ----------


def _message_update(update_id: int, user_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="u"),
            text="hi",
        ),
    )


def _slow_dispatcher(backend, log: list, name: str, delay: float = 0.4) -> Dispatcher:
    router = Router()

    @router.message()
    async def handle(message: Message) -> None:
        log.append((name, message.from_user.id, "start", asyncio.get_running_loop().time()))
        await asyncio.sleep(delay)
        log.append((name, message.from_user.id, "end", asyncio.get_running_loop().time()))

    dp = Dispatcher(storage=backend.storage, events_isolation=backend.isolation)
    dp.include_router(router)
    return dp


def _intervals(log, user_id):
    starts = sorted(t for _, u, kind, t in log if u == user_id and kind == "start")
    ends = sorted(t for _, u, kind, t in log if u == user_id and kind == "end")
    return list(zip(starts, ends))


async def test_two_processes_do_not_handle_updates_of_one_user_concurrently(redis_url, fsm_prefix):
    """Два Dispatcher на одном Redis = две реплики: апдейты одного пользователя выстраиваются в очередь."""
    log: list = []
    one, two = redis_backend(redis_url, fsm_prefix), redis_backend(redis_url, fsm_prefix)
    dp1, dp2 = _slow_dispatcher(one, log, "p1"), _slow_dispatcher(two, log, "p2")
    bot = Bot(token=f"{BOT_A}:TEST", session=RecordingSession())
    try:
        await asyncio.gather(dp1.feed_update(bot, _message_update(1, 42)), dp2.feed_update(bot, _message_update(2, 42)))
        (first_start, first_end), (second_start, _) = _intervals(log, 42)
        assert second_start >= first_end - 0.01, "второй апдейт того же пользователя начался, пока шёл первый"
    finally:
        await dp1.fsm.close()
        await dp2.fsm.close()


async def test_control_without_isolation_the_same_updates_do_overlap(redis_url, fsm_prefix):
    """Контроль теста выше: без изоляции событий те же два апдейта пересекаются — значит, тест умеет ловить гонку."""
    from aiogram.fsm.storage.memory import DisabledEventIsolation

    log: list = []
    backend = redis_backend(redis_url, fsm_prefix)
    backend.isolation = DisabledEventIsolation()
    dp = _slow_dispatcher(backend, log, "p1")
    bot = Bot(token=f"{BOT_A}:TEST", session=RecordingSession())
    try:
        await asyncio.gather(dp.feed_update(bot, _message_update(1, 42)), dp.feed_update(bot, _message_update(2, 42)))
        (_, first_end), (second_start, _) = _intervals(log, 42)
        assert second_start < first_end
    finally:
        await dp.fsm.close()


async def test_isolation_is_per_user_so_other_users_run_in_parallel(redis_url, fsm_prefix):
    log: list = []
    one, two = redis_backend(redis_url, fsm_prefix), redis_backend(redis_url, fsm_prefix)
    dp1, dp2 = _slow_dispatcher(one, log, "p1"), _slow_dispatcher(two, log, "p2")
    bot = Bot(token=f"{BOT_A}:TEST", session=RecordingSession())
    try:
        started = asyncio.get_running_loop().time()
        await asyncio.gather(dp1.feed_update(bot, _message_update(1, 42)), dp2.feed_update(bot, _message_update(2, 43)))
        assert asyncio.get_running_loop().time() - started < 0.75  # 2 × 0.4 с, если бы блокировка была общей
    finally:
        await dp1.fsm.close()
        await dp2.fsm.close()


async def test_same_user_in_different_bots_does_not_block(redis_url, fsm_prefix):
    log: list = []
    one, two = redis_backend(redis_url, fsm_prefix), redis_backend(redis_url, fsm_prefix)
    dp1, dp2 = _slow_dispatcher(one, log, "p1"), _slow_dispatcher(two, log, "p2")
    bot_a = Bot(token=f"{BOT_A}:TEST", session=RecordingSession())
    bot_b = Bot(token=f"{BOT_B}:TEST", session=RecordingSession())
    try:
        started = asyncio.get_running_loop().time()
        await asyncio.gather(dp1.feed_update(bot_a, _message_update(1, 42)), dp2.feed_update(bot_b, _message_update(2, 42)))
        assert asyncio.get_running_loop().time() - started < 0.75
    finally:
        await dp1.fsm.close()
        await dp2.fsm.close()


async def test_memory_mode_also_serializes_one_user_within_a_process():
    log: list = []
    memory = create_fsm_backend(kind="memory")
    dp = _slow_dispatcher(memory, log, "p1", delay=0.2)
    bot = Bot(token=f"{BOT_A}:TEST", session=RecordingSession())
    await asyncio.gather(dp.feed_update(bot, _message_update(1, 42)), dp.feed_update(bot, _message_update(2, 42)))
    (_, first_end), (second_start, _) = _intervals(log, 42)
    assert second_start >= first_end - 0.01
