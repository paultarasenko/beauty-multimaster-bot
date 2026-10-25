"""Этап 7: недоступный Redis. Fail fast при старте, аккуратная обработка во время работы, /health и /ready, закрытие соединений.

«Redis пропал и вернулся» имитируется TCP-прокси перед настоящим Redis (tests/tcp_proxy.py).
"""

import asyncio
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import uvicorn
from aiogram import Dispatcher
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.methods import AnswerCallbackQuery
from cryptography.fernet import Fernet

from app import main as app_main
from app.bot import fsm_storage
from app.bot.dispatcher import build_dispatcher, default_routers
from app.bot.fsm_storage import create_fsm_backend
from app.bot.middlewares.error_handling import GENERIC_ERROR_TEXT
from app.config.settings import settings
from app.database.session import make_session_factory
from app.log_context import LogContextFilter
from app.multibot import runner
from app.multibot.crypto import TokenCipher
from app.multibot.registry import BotRegistry
from app.multibot.webhook import create_webhook_app
from app.scheduler.service import SchedulerService
from tests.conftest import TEST_DATABASE_URL, set_owner
from tests.tcp_proxy import TcpProxy
from tests.test_multibot_registry import add_row
from tests.webhook_utils import FakeTelegram, message_update

A, BOT_A, OWNER_A, CLIENT = 1, 1001, 900, 5001
DEAD_REDIS_URL = "redis://:SuperSecretPw@127.0.0.1:6399/0"  # на этом порту никого нет; пароль нужен для проверки утечек
SECRET = "SuperSecretPw"


@pytest.fixture(autouse=True)
def _detach_routers():
    """Модульные роутеры подключаются к диспетчеру один раз: run_webhook/build_dispatcher в тесте их занимают — отпускаем."""
    yield
    for router in default_routers():
        router._parent_router = None


class Stand:
    def __init__(self, cipher, tg, registry, client, dp, row):
        self.cipher, self.tg, self.registry, self.client, self.dp, self.row = cipher, tg, registry, client, dp, row
        self._uid = 0

    async def send(self, text: str) -> httpx.Response:
        self._uid += 1
        return await self.client.post(
            f"/tg/{BOT_A}/{self.row.webhook_secret}",
            json=message_update(self._uid, CLIENT, text),
            headers={"X-Telegram-Bot-Api-Secret-Token": self.cipher.header_token(self.row.webhook_secret)},
        )

    async def press(self, data: str) -> httpx.Response:
        self._uid += 1
        payload = {
            "update_id": self._uid,
            "callback_query": {
                "id": str(self._uid),
                "from": {"id": CLIENT, "is_bot": False, "first_name": "c"},
                "chat_instance": "ci",
                "message": {"message_id": 1, "date": int(time.time()), "chat": {"id": CLIENT, "type": "private"}, "text": "m"},
                "data": data,
            },
        }
        return await self.client.post(
            f"/tg/{BOT_A}/{self.row.webhook_secret}",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": self.cipher.header_token(self.row.webhook_secret)},
        )


async def _make_stand(session, backend, *, session_factory=None):
    await set_owner(session, OWNER_A, A)
    cipher = TokenCipher(Fernet.generate_key().decode())
    row = await add_row(session, cipher, A, BOT_A)
    tg = FakeTelegram()
    factory = make_session_factory(TEST_DATABASE_URL)
    registry = BotRegistry(factory, cipher, http_session_factory=lambda: tg)
    await registry.load_all()
    dp = build_dispatcher(
        factory, storage=backend.storage, events_isolation=backend.isolation, workflow_data={"bot_registry": registry}
    )
    app = create_webhook_app(registry, dp, max_body_bytes=100_000, session_factory=session_factory or factory)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    stand = Stand(cipher, tg, registry, client, dp, row)
    stand.factories = [factory]
    return stand


async def _close_stand(stand: Stand):
    await stand.client.aclose()
    await stand.registry.close()
    for factory in stand.factories:
        await factory.kw["bind"].dispose()
    for router in default_routers():
        router._parent_router = None


@pytest_asyncio.fixture
async def proxy(redis_url):
    from sqlalchemy.engine import make_url

    target = make_url(redis_url)
    proxy = TcpProxy(target.host or "127.0.0.1", target.port or 6379)
    await proxy.start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def stand(session, proxy, fsm_prefix):
    """Webhook-приложение, чей Redis стоит за прокси (его можно «уронить» и «поднять»)."""
    backend = create_fsm_backend(kind="redis", redis_url=f"redis://127.0.0.1:{proxy.port}/0", state_ttl=600, prefix=fsm_prefix)
    stand = await _make_stand(session, backend)
    yield stand
    await _close_stand(stand)
    await backend.storage.close()


# ---------- во время работы ----------


async def test_redis_outage_during_work_gives_quick_200_generic_reply_and_context_log(stand, proxy, caplog):
    caplog.handler.addFilter(LogContextFilter())
    assert (await stand.send("/start")).status_code == 200
    ok_replies = len(stand.tg.texts_of(BOT_A))
    assert ok_replies >= 1

    await proxy.stop()  # Redis пропал
    started = time.monotonic()
    with caplog.at_level(logging.ERROR):
        response = await stand.send("/start")
    assert response.status_code == 200  # Telegram не повторяет доставку: шторма ретраев нет
    assert time.monotonic() - started < 3
    assert stand.tg.texts_of(BOT_A)[ok_replies:] == [GENERIC_ERROR_TEXT]  # пользователь получил общий ответ

    records = [r for r in caplog.records if r.name == "app.bot.middlewares.storage_guard"]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert (records[0].master_id, records[0].bot_id) == (A, BOT_A)  # контекст мастера и бота в логе
    assert records[0].exc_info is None  # без простыни трейсбека на каждый апдейт
    assert SECRET not in caplog.text


async def test_callback_during_outage_is_answered_with_alert(stand, proxy):
    await proxy.stop()
    response = await stand.press("any")
    assert response.status_code == 200
    answers = stand.tg.methods_of(BOT_A, AnswerCallbackQuery)
    assert [(a.text, a.show_alert) for a in answers] == [(GENERIC_ERROR_TEXT, True)]


async def test_service_recovers_when_redis_returns_and_state_is_kept(stand, proxy):
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=BOT_A, chat_id=CLIENT, user_id=CLIENT)
    await stand.dp.storage.set_state(key, "BookingFSM:select_date")
    port = proxy.port
    await proxy.stop()
    assert (await stand.send("/start")).status_code == 200  # во время отказа: общий ответ
    texts = stand.tg.texts_of(BOT_A)
    assert texts and texts[-1] == GENERIC_ERROR_TEXT

    await proxy.start(port)  # Redis вернулся — перезапуск приложения не нужен
    before = len(stand.tg.texts_of(BOT_A))
    assert (await stand.send("/start")).status_code == 200
    new_texts = stand.tg.texts_of(BOT_A)[before:]
    assert new_texts and GENERIC_ERROR_TEXT not in new_texts
    # состояние, записанное до отказа, пережило его: хранилище снова отвечает, данные на месте
    assert await stand.dp.storage.get_state(key) == "BookingFSM:select_date"


async def test_restart_of_redis_between_updates_costs_no_user_error(stand, proxy):
    """Быстрый перезапуск Redis: в пуле осталось «протухшее» соединение — одна повторная попытка скрывает это от пользователя."""
    assert (await stand.send("/start")).status_code == 200
    port = proxy.port
    await proxy.stop()
    await proxy.start(port)
    before = len(stand.tg.texts_of(BOT_A))
    assert (await stand.send("/start")).status_code == 200
    assert GENERIC_ERROR_TEXT not in stand.tg.texts_of(BOT_A)[before:]


async def test_waiting_for_a_busy_user_lock_times_out_with_generic_reply(session, redis_url, fsm_prefix):
    """Апдейт того же пользователя ждёт блокировку не вечно: по таймауту — общий ответ, а не зависание запроса к Telegram."""
    from aiogram.fsm.storage.base import StorageKey

    backend = create_fsm_backend(kind="redis", redis_url=redis_url, prefix=fsm_prefix)
    backend.isolation = RedisEventIsolation(  # ждём всего 0.3 с вместо штатных 30
        backend.storage.redis, backend.storage.key_builder, lock_kwargs={"timeout": 5, "blocking_timeout": 0.3}
    )
    stand = await _make_stand(session, backend)
    try:
        key = StorageKey(bot_id=BOT_A, chat_id=CLIENT, user_id=CLIENT)
        started = time.monotonic()
        async with backend.isolation.lock(key):  # «другая реплика» обрабатывает апдейт этого пользователя
            response = await stand.send("/start")
        assert response.status_code == 200 and time.monotonic() - started < 3
        assert stand.tg.texts_of(BOT_A) == [GENERIC_ERROR_TEXT]
    finally:
        await _close_stand(stand)
        await backend.storage.close()


# ---------- /health и /ready ----------


async def test_ready_ok_when_db_and_redis_respond(stand):
    response = await stand.client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "db": "ok", "redis": "ok"}


async def test_ready_fails_when_redis_is_down_but_health_stays_ok(stand, proxy):
    port = proxy.port
    await proxy.stop()
    response = await stand.client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "db": "ok", "redis": "fail"}
    assert SECRET not in response.text and "6399" not in response.text and str(port) not in response.text
    health = await stand.client.get("/health")  # liveness: процесс жив, зависимости не проверяются
    assert health.status_code == 200 and health.json() == {"status": "ok"}

    await proxy.start(port)
    assert (await stand.client.get("/ready")).status_code == 200


async def test_ready_fails_when_database_is_down(session, redis_url, fsm_prefix):
    backend = create_fsm_backend(kind="redis", redis_url=redis_url, prefix=fsm_prefix)
    broken = make_session_factory("postgresql+asyncpg://postgres:postgres@127.0.0.1:1/none_test")
    stand = await _make_stand(session, backend, session_factory=broken)
    try:
        response = await stand.client.get("/ready")
        assert response.status_code == 503 and response.json() == {"status": "not_ready", "db": "fail", "redis": "ok"}
    finally:
        await broken.kw["bind"].dispose()
        await _close_stand(stand)
        await backend.storage.close()


async def test_ready_with_memory_storage_does_not_need_redis(session):
    backend = create_fsm_backend(kind="memory")
    stand = await _make_stand(session, backend)
    try:
        response = await stand.client.get("/ready")
        assert response.status_code == 200 and response.json() == {"status": "ready", "db": "ok", "redis": "skipped"}
    finally:
        await _close_stand(stand)


# ---------- старт: fail fast ----------


async def _run_webhook() -> None:
    factory = make_session_factory(TEST_DATABASE_URL)
    try:
        await runner.run_webhook(factory)
    finally:
        await factory.kw["bind"].dispose()


@pytest.fixture
def webhook_settings(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "webhook")
    monkeypatch.setattr(settings, "fsm_storage", None)
    monkeypatch.setattr(settings, "bot_token_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.com")
    monkeypatch.setattr(settings, "redis_url", DEAD_REDIS_URL)


async def test_webhook_start_fails_fast_when_redis_is_unavailable(session, webhook_settings, monkeypatch):
    served, scheduler_started = [], []

    async def fake_serve(self, *args, **kwargs):
        served.append(True)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(SchedulerService, "start", lambda self: scheduler_started.append(True))

    started = time.monotonic()
    with pytest.raises(SystemExit) as exc:
        await _run_webhook()
    message = str(exc.value)
    assert "Redis" in message and "127.0.0.1:6399" in message  # понятная причина и адрес
    assert SECRET not in message  # пароль из REDIS_URL не печатается
    assert not served and not scheduler_started  # сервер и планировщик не запускались
    assert time.monotonic() - started < 5


async def test_webhook_start_with_redis_checks_it_then_serves_and_closes_connections(
    session, redis_url, webhook_settings, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "redis_url", redis_url)
    served = []
    closed = []

    async def fake_serve(self, *args, **kwargs):
        served.append(True)

    original_close = RedisStorage.close

    async def spy_close(self):
        closed.append(True)
        await original_close(self)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(RedisStorage, "close", spy_close)
    with caplog.at_level(logging.INFO, logger="app.multibot.runner"):
        await _run_webhook()
    assert served == [True]
    assert closed  # соединения FSM закрыты при остановке
    assert any("Хранилище FSM: redis" in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text and redis_url not in caplog.text


async def test_webhook_explicit_memory_starts_without_redis(session, webhook_settings, monkeypatch, caplog):
    monkeypatch.setattr(settings, "fsm_storage", "memory")  # Redis (недоступный) не нужен
    served = []

    async def fake_serve(self, *args, **kwargs):
        served.append(True)

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    with caplog.at_level(logging.WARNING):
        await _run_webhook()
    assert served == [True]
    assert any("FSM_STORAGE=memory" in r.getMessage() for r in caplog.records)


async def test_polling_with_redis_fails_fast_when_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "polling")
    monkeypatch.setattr(settings, "fsm_storage", "redis")
    monkeypatch.setattr(settings, "redis_url", DEAD_REDIS_URL)
    monkeypatch.setattr(settings, "bot_token", "123456:TEST")

    async def tenant(_):
        return SimpleNamespace(master_id=1)

    async def noop(_):
        return None

    polled = []

    async def fake_polling(self, *args, **kwargs):
        polled.append(True)

    monkeypatch.setattr(app_main, "ensure_tenant_available", tenant)
    monkeypatch.setattr(app_main, "bootstrap_owner", noop)
    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    with pytest.raises(SystemExit) as exc:
        await app_main.run_polling()
    assert "Redis" in str(exc.value) and SECRET not in str(exc.value)
    assert not polled


# ---------- закрытие соединений ----------


async def test_lifespan_closes_fsm_connections_and_double_close_is_safe(session, redis_url, fsm_prefix, monkeypatch):
    backend = create_fsm_backend(kind="redis", redis_url=redis_url, prefix=fsm_prefix)
    stand = await _make_stand(session, backend)
    closed = []
    original = RedisStorage.close

    async def spy(self):
        closed.append(True)
        await original(self)

    monkeypatch.setattr(RedisStorage, "close", spy)
    try:
        app = stand.client._transport.app
        async with app.router.lifespan_context(app):
            assert (await stand.client.get("/ready")).status_code == 200
        assert closed == [True]  # остановка приложения закрыла хранилище
        await fsm_storage.close_fsm(stand.dp)  # повторное закрытие (runner делает его в finally) не падает
    finally:
        await _close_stand(stand)


async def test_polling_shutdown_closes_redis_via_dispatcher(redis_url, fsm_prefix, monkeypatch):
    """В polling закрывает aiogram: emit_shutdown → fsm.close → RedisStorage.close (отдельного кода закрытия не нужно)."""
    backend = create_fsm_backend(kind="redis", redis_url=redis_url, prefix=fsm_prefix)
    dp = build_dispatcher(None, storage=backend.storage, events_isolation=backend.isolation, routers=[])
    await fsm_storage.ensure_fsm_ready(dp.storage)
    closed = []
    original = RedisStorage.close

    async def spy(self):
        closed.append(True)
        await original(self)

    monkeypatch.setattr(RedisStorage, "close", spy)
    await dp.emit_shutdown()
    assert closed == [True]
