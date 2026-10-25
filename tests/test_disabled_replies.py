"""Вежливые ответы отключённому боту (этап 10): антиспам раз в 6 часов, фразы клиенту/владельцу, сбои, Redis.

Три уровня:
1. `DisabledReplyThrottle` — память (с поддельными часами), поддельный Redis, настоящий Redis (пропускается, если его нет).
2. `reply_to_disabled_update` — с поддельным ботом, без БД и без сети.
3. Сквозные тесты через webhook: оба пути (реестр знает об отключении / реестр ещё считает мастера активным) делят один антиспам.
"""

import datetime as dt
import uuid

import httpx
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select, update

from app.bot import disabled_replies
from app.bot.disabled_replies import (
    CLIENT_TEXT,
    KEY_PREFIX,
    OWNER_TEXT,
    REPLY_INTERVAL_SECONDS,
    SEND_TIMEOUT_SECONDS,
    DisabledReplyThrottle,
    reply_to_disabled_update,
)
from app.bot.dispatcher import build_dispatcher, default_routers
from app.bot.fsm_storage import create_fsm_backend
from app.database.models import Master, User
from app.database.session import make_session_factory
from app.multibot.crypto import TokenCipher
from app.multibot.registry import BotRegistry
from app.multibot.webhook import create_webhook_app
from tests.conftest import TEST_DATABASE_URL, set_owner
from tests.test_multibot_registry import add_master, add_row
from tests.test_multibot_webhook import A, B, BOT_A, BOT_B, CLIENT, OWNER_A, OWNER_B, Env
from tests.webhook_utils import FakeTelegram

BOT_ID = 4242
USER_ID = 5001
OWNER_ID = 900
CHAT_ID = 5001


# ---------- вспомогательные поддельные объекты ----------


class FakeClock:
    """Управляемые часы для антиспама в памяти."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBot:
    """Только то, что трогает reply_to_disabled_update: id, send_message, answer_callback_query."""

    def __init__(self, bot_id: int = BOT_ID, fail_with: Exception | None = None) -> None:
        self.id = bot_id
        self.fail_with = fail_with
        self.messages: list[tuple] = []
        self.callbacks: list[tuple] = []

    async def send_message(self, chat_id, text, request_timeout=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.messages.append((chat_id, text, request_timeout))

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=None, request_timeout=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.callbacks.append((callback_query_id, text, show_alert, request_timeout))


class OwnerLookup:
    """Поддельный `owner_telegram_id`: считает обращения (в «БД» ходим только когда ответ реально уйдёт)."""

    def __init__(self, owner_id: int | None = OWNER_ID, error: Exception | None = None) -> None:
        self.owner_id, self.error, self.calls = owner_id, error, 0

    async def __call__(self) -> int | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.owner_id


class RecordingRedis:
    """Поддельный Redis: SET NX EX с запоминанием вызовов; повторный SET того же ключа возвращает None, как настоящий."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.keys: set[str] = set()

    async def set(self, key, value, nx=False, ex=None):
        self.calls.append((key, value, nx, ex))
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


class DownRedis:
    async def set(self, *args, **kwargs):
        raise RedisConnectionError("Redis down")


def tg_user(user_id: int = USER_ID, is_bot: bool = False) -> TgUser:
    return TgUser(id=user_id, is_bot=is_bot, first_name=f"user{user_id}")


def message_obj(user_id: int = USER_ID, chat_type: str = "private", is_bot: bool = False, with_user: bool = True) -> Message:
    return Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=CHAT_ID if chat_type == "private" else -100500, type=chat_type),
        from_user=tg_user(user_id, is_bot) if with_user else None,
        text="/start",
    )


def message_update_obj(**kwargs) -> Update:
    return Update(update_id=1, message=message_obj(**kwargs))


def callback_update_obj(user_id: int = USER_ID, callback_id: str = "cb-1") -> Update:
    callback = CallbackQuery(id=callback_id, from_user=tg_user(user_id), chat_instance="ci", data="whatever")
    return Update(update_id=2, callback_query=callback)


# ---------- 1. антиспам в памяти ----------


async def test_memory_first_allowed_then_blocked_for_same_pair():
    throttle = DisabledReplyThrottle(clock=FakeClock())
    assert await throttle.allow(BOT_ID, USER_ID) is True
    assert await throttle.allow(BOT_ID, USER_ID) is False
    assert await throttle.allow(BOT_ID, USER_ID) is False


async def test_memory_throttle_is_per_bot_and_per_user():
    throttle = DisabledReplyThrottle(clock=FakeClock())
    assert await throttle.allow(BOT_ID, USER_ID) is True
    assert await throttle.allow(BOT_ID, USER_ID + 1) is True  # другой пользователь того же бота
    assert await throttle.allow(BOT_ID + 1, USER_ID) is True  # тот же пользователь у другого бота (другой мастер)
    assert await throttle.allow(BOT_ID, USER_ID) is False


async def test_memory_interval_boundary():
    clock = FakeClock()
    throttle = DisabledReplyThrottle(clock=clock)
    assert await throttle.allow(BOT_ID, USER_ID) is True
    clock.advance(REPLY_INTERVAL_SECONDS - 1)
    assert await throttle.allow(BOT_ID, USER_ID) is False  # за секунду до конца интервала — ещё нельзя
    clock.advance(1)
    assert await throttle.allow(BOT_ID, USER_ID) is True  # интервал прошёл ровно — можно
    assert await throttle.allow(BOT_ID, USER_ID) is False  # и снова отсчёт с нуля


def test_default_interval_is_six_hours():
    assert REPLY_INTERVAL_SECONDS == 6 * 3600


async def test_memory_custom_interval_is_respected():
    clock = FakeClock()
    throttle = DisabledReplyThrottle(interval_seconds=60, clock=clock)
    assert await throttle.allow(BOT_ID, USER_ID) is True
    clock.advance(59)
    assert await throttle.allow(BOT_ID, USER_ID) is False
    clock.advance(1)
    assert await throttle.allow(BOT_ID, USER_ID) is True


async def test_memory_overflow_forgets_only_stale_entries(monkeypatch):
    """При достижении лимита записей протухшие забываются, свежие — нет (иначе антиспам можно было бы сбросить потоком новых людей)."""
    monkeypatch.setattr(disabled_replies, "MAX_TRACKED_USERS", 3)
    clock = FakeClock()
    throttle = DisabledReplyThrottle(clock=clock)
    for uid in (1, 2, 3):
        assert await throttle.allow(BOT_ID, uid) is True
    clock.advance(REPLY_INTERVAL_SECONDS + 1)
    assert await throttle.allow(BOT_ID, 4) is True  # лимит достигнут: старые три протухли и забыты
    assert len(throttle._last) == 1
    assert await throttle.allow(BOT_ID, 4) is False  # свежая запись сохранилась


async def test_memory_overflow_keeps_fresh_entries(monkeypatch):
    monkeypatch.setattr(disabled_replies, "MAX_TRACKED_USERS", 3)
    throttle = DisabledReplyThrottle(clock=FakeClock())
    for uid in (1, 2, 3):
        assert await throttle.allow(BOT_ID, uid) is True
    assert await throttle.allow(BOT_ID, 4) is True  # все свежие — забывать нечего
    assert await throttle.allow(BOT_ID, 1) is False  # первый пользователь по-прежнему заблокирован антиспамом


# ---------- 1b. антиспам через Redis (поддельный) ----------


async def test_redis_uses_set_nx_ex_with_documented_key():
    fake = RecordingRedis()
    throttle = DisabledReplyThrottle(fake, interval_seconds=123)
    assert await throttle.allow(BOT_ID, USER_ID) is True
    assert await throttle.allow(BOT_ID, USER_ID) is False
    expected_key = f"{KEY_PREFIX}:{BOT_ID}:{USER_ID}"
    assert KEY_PREFIX == "bbot:disabled_reply"
    assert fake.calls == [(expected_key, "1", True, 123)] * 2


async def test_redis_down_means_silence_and_warning_not_exception(caplog):
    throttle = DisabledReplyThrottle(DownRedis())
    with caplog.at_level("WARNING"):
        assert await throttle.allow(BOT_ID, USER_ID) is False
    assert any("Redis недоступен" in r.getMessage() and "ConnectionError" in r.getMessage() for r in caplog.records)


def test_for_storage_picks_memory_or_redis():
    assert DisabledReplyThrottle.for_storage(MemoryStorage())._redis is None
    redis = Redis.from_url("redis://localhost:6380/0")  # соединение ленивое — подключаться не будем
    storage = RedisStorage(redis)
    assert DisabledReplyThrottle.for_storage(storage)._redis is storage.redis


# ---------- 1c. антиспам через настоящий Redis ----------


@pytest_asyncio.fixture
async def real_redis(redis_url):
    client = Redis.from_url(redis_url)
    created: list[str] = []
    try:
        yield client, created
    finally:
        for key in created:  # чистим только свои ключи, FLUSHDB не вызываем
            await client.delete(key)
        await client.aclose()


async def test_real_redis_blocks_second_reply_sets_ttl_and_is_shared_between_processes(real_redis):
    client, created = real_redis
    bot_id = 10**10 + uuid.uuid4().int % 10**9  # уникальный бот: ключ не пересекается с другими тестами и прогонами
    key = f"{KEY_PREFIX}:{bot_id}:{USER_ID}"
    created.append(key)
    first_process = DisabledReplyThrottle(client, interval_seconds=100)
    second_process = DisabledReplyThrottle(client, interval_seconds=100)  # «другой процесс» с тем же Redis

    assert await first_process.allow(bot_id, USER_ID) is True
    assert 0 < await client.ttl(key) <= 100  # ключ с TTL, а не вечный
    assert await first_process.allow(bot_id, USER_ID) is False
    assert await second_process.allow(bot_id, USER_ID) is False  # антиспам общий для процессов
    other_key = f"{KEY_PREFIX}:{bot_id}:{USER_ID + 1}"
    created.append(other_key)
    assert await second_process.allow(bot_id, USER_ID + 1) is True


# ---------- 2. reply_to_disabled_update ----------


async def test_client_message_gets_client_text():
    bot, owner = FakeBot(), OwnerLookup(OWNER_ID)
    assert await reply_to_disabled_update(bot, message_update_obj(), DisabledReplyThrottle(), owner) is True
    assert bot.messages == [(CHAT_ID, CLIENT_TEXT, SEND_TIMEOUT_SECONDS)]
    assert bot.callbacks == []


async def test_owner_message_gets_owner_text():
    bot, owner = FakeBot(), OwnerLookup(OWNER_ID)
    update_ = message_update_obj(user_id=OWNER_ID)
    assert await reply_to_disabled_update(bot, update_, DisabledReplyThrottle(), owner) is True
    assert bot.messages == [(CHAT_ID, OWNER_TEXT, SEND_TIMEOUT_SECONDS)]


def test_owner_and_client_texts_differ_and_owner_text_hides_details():
    assert CLIENT_TEXT != OWNER_TEXT
    assert "администратору сервиса" in OWNER_TEXT
    assert "мастером" in CLIENT_TEXT


async def test_master_without_owner_treats_everyone_as_client():
    bot = FakeBot()
    await reply_to_disabled_update(bot, message_update_obj(), DisabledReplyThrottle(), OwnerLookup(None))
    assert [m[1] for m in bot.messages] == [CLIENT_TEXT]


async def test_callback_is_answered_with_alert_and_no_message_is_sent():
    bot = FakeBot()
    assert await reply_to_disabled_update(bot, callback_update_obj(), DisabledReplyThrottle(), OwnerLookup()) is True
    assert bot.callbacks == [("cb-1", CLIENT_TEXT, True, SEND_TIMEOUT_SECONDS)]
    assert bot.messages == []


async def test_owner_callback_gets_owner_text():
    bot = FakeBot()
    await reply_to_disabled_update(bot, callback_update_obj(user_id=OWNER_ID), DisabledReplyThrottle(), OwnerLookup(OWNER_ID))
    assert [c[1] for c in bot.callbacks] == [OWNER_TEXT]


async def test_second_update_within_interval_is_silent_and_skips_owner_lookup():
    bot, owner, throttle = FakeBot(), OwnerLookup(), DisabledReplyThrottle(clock=FakeClock())
    assert await reply_to_disabled_update(bot, message_update_obj(), throttle, owner) is True
    assert await reply_to_disabled_update(bot, message_update_obj(), throttle, owner) is False
    assert await reply_to_disabled_update(bot, callback_update_obj(), throttle, owner) is False  # антиспам общий для сообщений и кнопок
    assert len(bot.messages) == 1 and bot.callbacks == []
    assert owner.calls == 1  # к БД обращались только когда ответ действительно уходил


async def test_reply_allowed_again_after_interval():
    clock = FakeClock()
    bot, throttle = FakeBot(), DisabledReplyThrottle(clock=clock)
    await reply_to_disabled_update(bot, message_update_obj(), throttle, OwnerLookup())
    clock.advance(REPLY_INTERVAL_SECONDS)
    assert await reply_to_disabled_update(bot, message_update_obj(), throttle, OwnerLookup()) is True
    assert len(bot.messages) == 2


async def test_same_user_gets_reply_from_each_disabled_bot():
    throttle = DisabledReplyThrottle(clock=FakeClock())
    bot_a, bot_b = FakeBot(1), FakeBot(2)
    assert await reply_to_disabled_update(bot_a, message_update_obj(), throttle, OwnerLookup()) is True
    assert await reply_to_disabled_update(bot_b, message_update_obj(), throttle, OwnerLookup()) is True


@pytest.mark.parametrize(
    "update_",
    [
        pytest.param(message_update_obj(chat_type="group"), id="group-chat"),
        pytest.param(message_update_obj(with_user=False), id="no-author"),
        pytest.param(Update(update_id=3, edited_message=message_obj()), id="edited-message"),
    ],
)
async def test_updates_without_private_author_are_dropped_silently(update_):
    bot, owner = FakeBot(), OwnerLookup()
    assert await reply_to_disabled_update(bot, update_, DisabledReplyThrottle(), owner) is False
    assert bot.messages == [] and bot.callbacks == [] and owner.calls == 0


async def test_bot_author_is_ignored_and_does_not_consume_throttle():
    bot, throttle = FakeBot(), DisabledReplyThrottle(clock=FakeClock())
    assert await reply_to_disabled_update(bot, message_update_obj(is_bot=True), throttle, OwnerLookup()) is False
    assert bot.messages == []
    assert await throttle.allow(bot.id, USER_ID) is True  # отметка в антиспаме не ставилась


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("network down"), id="generic"),
        pytest.param(
            TelegramForbiddenError(method=SendMessage(chat_id=CHAT_ID, text="x"), message="Forbidden: bot was blocked by the user"),
            id="user-blocked-bot",
        ),
    ],
)
async def test_send_failure_is_swallowed_logged_and_returns_false(error, caplog):
    bot = FakeBot(fail_with=error)
    with caplog.at_level("WARNING"):
        result = await reply_to_disabled_update(bot, message_update_obj(), DisabledReplyThrottle(), OwnerLookup())
    assert result is False
    assert any("Не удалось ответить отключённому боту" in r.getMessage() and type(error).__name__ in r.getMessage() for r in caplog.records)


async def test_callback_answer_failure_is_swallowed(caplog):
    bot = FakeBot(fail_with=RuntimeError("boom"))
    with caplog.at_level("WARNING"):
        assert await reply_to_disabled_update(bot, callback_update_obj(), DisabledReplyThrottle(), OwnerLookup()) is False


async def test_owner_lookup_failure_is_swallowed_and_nothing_is_sent():
    bot = FakeBot()
    owner = OwnerLookup(error=RuntimeError("db down"))
    assert await reply_to_disabled_update(bot, message_update_obj(), DisabledReplyThrottle(), owner) is False
    assert bot.messages == []


async def test_redis_down_means_no_reply_and_no_owner_lookup():
    bot, owner = FakeBot(), OwnerLookup()
    throttle = DisabledReplyThrottle(DownRedis())
    assert await reply_to_disabled_update(bot, message_update_obj(), throttle, owner) is False
    assert bot.messages == [] and owner.calls == 0  # лучше молчание, чем спам


# ---------- 3. сквозные тесты через webhook ----------


@pytest_asyncio.fixture
async def decline_env(session):
    """Как `env` из test_multibot_webhook, но с `session_factory` у webhook-приложения (в проде он есть) и явной памятью для FSM:
    без session_factory ветка «реестр знает об отключении» молчит, а настоящий Redis сделал бы антиспам общим между прогонами."""
    cipher = TokenCipher(Fernet.generate_key().decode())
    await add_master(session, B, OWNER_B)
    await set_owner(session, OWNER_A, A)
    rows = {BOT_A: await add_row(session, cipher, A, BOT_A), BOT_B: await add_row(session, cipher, B, BOT_B)}
    tg = FakeTelegram()
    factory = make_session_factory(TEST_DATABASE_URL)
    registry = BotRegistry(factory, cipher, http_session_factory=lambda: tg)
    await registry.load_all()
    backend = create_fsm_backend(kind="memory")
    dp = build_dispatcher(
        factory,
        storage=backend.storage,
        events_isolation=backend.isolation,
        workflow_data={"bot_registry": registry},
    )
    app = create_webhook_app(registry, dp, max_body_bytes=100_000, session_factory=factory)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        yield Env(cipher, tg, registry, client, rows)
    finally:
        await client.aclose()
        await registry.close()
        for router in default_routers():
            router._parent_router = None  # модульные роутеры подключаются к диспетчеру один раз


async def _disable_master_b(session) -> None:
    await session.execute(update(Master).where(Master.id == B).values(is_active=False))
    await session.commit()


async def test_webhook_registry_path_replies_to_client_once_and_not_again(decline_env, session):
    env = decline_env
    await _disable_master_b(session)
    await env.registry.refresh(BOT_B)  # реестр знает: мастер отключён

    first = await env.send(BOT_B, CLIENT, "/start")
    second = await env.send(BOT_B, CLIENT, "/start")
    assert first.status_code == 200 and second.status_code == 200
    assert env.tg.texts_of(BOT_B) == [CLIENT_TEXT]  # второе сообщение — тишина
    assert (await session.execute(select(User).where(User.telegram_id == CLIENT))).scalar_one_or_none() is None

    await env.send(BOT_B, CLIENT + 1, "/start")  # другой человек — свой ответ
    assert env.tg.texts_of(BOT_B) == [CLIENT_TEXT, CLIENT_TEXT]


async def test_webhook_owner_gets_owner_text_not_client_text(decline_env, session):
    env = decline_env
    await _disable_master_b(session)
    await env.registry.refresh(BOT_B)
    response = await env.send(BOT_B, OWNER_B, "/start")
    assert response.status_code == 200
    assert env.tg.texts_of(BOT_B) == [OWNER_TEXT]


async def test_webhook_other_masters_bot_keeps_working(decline_env, session):
    env = decline_env
    await _disable_master_b(session)
    await env.registry.refresh(BOT_B)
    await env.send(BOT_B, CLIENT, "/start")
    await env.send(BOT_A, CLIENT, "/start")
    assert CLIENT_TEXT not in env.tg.texts_of(BOT_A)
    assert env.tg.texts_of(BOT_A)  # бот A отвечает по-обычному


async def test_webhook_both_paths_share_one_throttle(decline_env, session):
    """Сначала ответ дал TenantMiddleware (реестр устарел), потом реестр обновили — второй путь не должен ответить повторно."""
    env = decline_env
    await _disable_master_b(session)
    await env.send(BOT_B, CLIENT, "/start")  # кэш реестра не обновляли → ответ из TenantMiddleware
    assert env.tg.texts_of(BOT_B) == [CLIENT_TEXT]

    await env.registry.refresh(BOT_B)  # теперь реестр знает об отключении → ветка webhook
    await env.send(BOT_B, CLIENT, "/start")
    assert env.tg.texts_of(BOT_B) == [CLIENT_TEXT]  # по-прежнему один ответ
