"""Этап 11, webhook: апдейт попадает только к мастеру своего бота; чужие/неверные секреты и пути отклоняются без подробностей;
после ротации секрета старый путь мёртв; суперадмин-путь /sa/{secret} работает только со своим секретом.
"""

from types import SimpleNamespace

import httpx
import pytest_asyncio
from aiogram import Bot
from sqlalchemy import update

from app.bot.fsm_storage import create_fsm_backend
from app.bot.dispatcher import build_dispatcher, default_routers
from app.database.models import Master, MasterBot
from app.database.session import make_session_factory
from app.multibot import management
from app.multibot.registry import BotRegistry
from app.multibot.webhook import SECRET_HEADER, create_webhook_app
from tests.conftest import TEST_DATABASE_URL
from tests.stage11_support import A, B, BOT_ID, ONLY_TG, SHARED_TG, TAG, snapshot, two_masters  # noqa: F401  (фикстура pytest)
from tests.webhook_utils import FakeTelegram, message_update

SA_PATH, SA_HEADER = "sa-path-secret-1", "sa-header-token-1"
SERVICES_BTN = "💰 Услуги и цены"


class StubDispatcher:
    def __init__(self):
        self.updates = []

    async def feed_update(self, bot, update_):
        self.updates.append(update_)


class Env:
    def __init__(self, ns, tg, registry, client, sa, factory):
        self.ns, self.tg, self.registry, self.client, self.sa, self.factory = ns, tg, registry, client, sa, factory
        self._uid = 0

    def secret(self, master: int) -> str:
        return self.ns.webhook_secret[master]

    def header(self, secret: str) -> str:
        return self.ns.cipher.header_token(secret)

    async def post(self, bot_id, path_secret, user_id, text, *, header=None):
        self._uid += 1
        headers = {} if header is None else {SECRET_HEADER: header}
        return await self.client.post(f"/tg/{bot_id}/{path_secret}", json=message_update(self._uid, user_id, text), headers=headers)

    async def send(self, master: int, user_id: int, text: str):
        """Корректный запрос в бот мастера: свой путь и свой заголовок."""
        secret = self.secret(master)
        return await self.post(BOT_ID[master], secret, user_id, text, header=self.header(secret))

    def shown(self, master: int) -> str:
        return "\n".join(self.tg.texts_of(BOT_ID[master]))


@pytest_asyncio.fixture
async def env(session, two_masters):
    ns = two_masters
    tg = FakeTelegram()
    factory = make_session_factory(TEST_DATABASE_URL)
    registry = BotRegistry(factory, ns.cipher, http_session_factory=lambda: tg)
    await registry.load_all()
    backend = create_fsm_backend(kind="memory")  # не зависит от REDIS_URL/RUN_MODE локального .env
    dp = build_dispatcher(factory, storage=backend.storage, events_isolation=backend.isolation, workflow_data={"bot_registry": registry})
    sa = SimpleNamespace(
        bot=Bot("777000:" + "S" * 35, session=tg), dp=StubDispatcher(), path_secret=SA_PATH, header_token=SA_HEADER
    )
    app = create_webhook_app(registry, dp, max_body_bytes=100_000, session_factory=factory, superadmin=sa)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        yield Env(ns, tg, registry, client, sa, factory)
    finally:
        await client.aclose()
        await registry.close()
        for router in default_routers():
            router._parent_router = None
        await factory.kw["bind"].dispose()


# ---------------------------------------------------------------- апдейт обрабатывается как мастер своего бота


async def test_update_on_path_of_a_is_handled_by_bot_a_in_context_of_master_a(env):
    response = await env.send(A, SHARED_TG, SERVICES_BTN)
    assert response.status_code == 200
    assert f"Услуга-{TAG[A]}" in env.shown(A) and f"Услуга-{TAG[B]}" not in env.shown(A)
    assert env.tg.per_bot.get(BOT_ID[B]) is None  # бот B вообще ничего не делал


async def test_each_bot_answers_with_own_masters_data(env):
    await env.send(A, SHARED_TG, SERVICES_BTN)
    await env.send(B, SHARED_TG, SERVICES_BTN)
    assert f"Услуга-{TAG[B]}" not in env.shown(A) and f"Услуга-{TAG[A]}" not in env.shown(B)
    assert f"Услуга-{TAG[B]}" in env.shown(B)


async def test_updates_to_bot_a_never_change_data_of_master_b(env, session):
    before_b = await snapshot(session, B)
    for person in (SHARED_TG, ONLY_TG[B], ONLY_TG[A]):
        for text in ("/start", SERVICES_BTN, "📋 Мои записи", "📞 Связаться со мной"):
            assert (await env.send(A, person, text)).status_code == 200
    assert await snapshot(session, B) == before_b
    assert f"Услуга-{TAG[B]}" not in env.shown(A) and f"Мастер-{TAG[B]}" not in env.shown(A)


# ---------------------------------------------------------------- отказы без раскрытия причины


async def test_wrong_secrets_and_unknown_bots_are_rejected_without_details(env):
    sa_secret, sb_secret = env.secret(A), env.secret(B)
    good_header_a = env.header(sa_secret)
    cases = [
        (BOT_ID[A], "wrong-secret", good_header_a),
        (BOT_ID[A], sb_secret, env.header(sb_secret)),  # секрет B на пути A (даже с заголовком B)
        (BOT_ID[B], sa_secret, good_header_a),
        (999999, sa_secret, good_header_a),  # несуществующий бот
    ]
    for bot_id, secret, header in cases:
        response = await env.post(bot_id, secret, SHARED_TG, "/start", header=header)
        assert response.status_code == 404 and response.content == b""
    bad_id = await env.client.post(f"/tg/abc/{sa_secret}", json=message_update(1, SHARED_TG, "/start"))
    assert bad_id.status_code == 404 and bad_id.content == b""
    assert env.tg.per_bot == {}  # ни один бот ничего не обработал


async def test_header_of_b_on_path_of_a_is_rejected(env):
    secret_a = env.secret(A)
    for header in (env.header(env.secret(B)), None, "bad", secret_a):
        response = await env.post(BOT_ID[A], secret_a, SHARED_TG, "/start", header=header)
        assert response.status_code == 403 and response.content == b""
    assert env.tg.per_bot == {}


async def test_disabled_master_or_bot_does_not_process_and_does_not_affect_neighbour(env, session):
    await session.execute(update(Master).where(Master.id == B).values(is_active=False))
    await session.commit()
    await env.registry.refresh(BOT_ID[B])
    before_b = await snapshot(session, B)
    assert (await env.send(B, SHARED_TG, "/start")).status_code == 200
    assert await snapshot(session, B) == before_b
    assert (await env.send(A, SHARED_TG, SERVICES_BTN)).status_code == 200
    assert f"Услуга-{TAG[A]}" in env.shown(A)

    await session.execute(update(MasterBot).where(MasterBot.master_id == A).values(is_active=False))
    await session.commit()
    await env.registry.refresh(BOT_ID[A])
    env.tg.per_bot.clear()
    assert (await env.send(A, SHARED_TG, SERVICES_BTN)).status_code == 200
    assert env.tg.per_bot == {}  # неактивный бот: апдейт отброшен


# ---------------------------------------------------------------- ротация секрета


async def test_after_secret_rotation_old_path_stops_working(env):
    old_secret = env.secret(A)
    assert (await env.send(A, SHARED_TG, SERVICES_BTN)).status_code == 200 and env.shown(A)

    rotated = await management.refresh_webhook(
        env.factory,
        env.ns.cipher,
        master_id=A,
        public_base_url="https://example.com",
        rotate_secret=True,
        bot_factory=lambda token: Bot(token, session=FakeTelegram()),
    )
    assert rotated.webhook_secret != old_secret
    await env.registry.refresh(BOT_ID[A])
    env.tg.per_bot.clear()

    old = await env.post(BOT_ID[A], old_secret, SHARED_TG, "/start", header=env.header(old_secret))
    assert old.status_code == 404 and old.content == b""
    assert env.tg.per_bot == {}
    new = await env.post(BOT_ID[A], rotated.webhook_secret, SHARED_TG, SERVICES_BTN, header=env.header(rotated.webhook_secret))
    assert new.status_code == 200 and env.shown(A)
    # Ротация у A не тронула путь B.
    assert (await env.send(B, SHARED_TG, SERVICES_BTN)).status_code == 200 and env.shown(B)


# ---------------------------------------------------------------- суперадмин-путь


async def test_superadmin_path_works_only_with_its_own_secret_and_header(env):
    payload = message_update(1, 12345, "/start")
    ok = await env.client.post(f"/sa/{SA_PATH}", json=payload, headers={SECRET_HEADER: SA_HEADER})
    assert ok.status_code == 200 and len(env.sa.dp.updates) == 1

    for secret in (env.secret(A), env.secret(B), "wrong", BOT_ID[A]):  # секреты мастеров и мусор — 404
        response = await env.client.post(f"/sa/{secret}", json=payload, headers={SECRET_HEADER: SA_HEADER})
        assert response.status_code == 404 and response.content == b""
    for header in (None, "bad", env.header(env.secret(A))):  # верный путь, чужой заголовок — 403
        headers = {} if header is None else {SECRET_HEADER: header}
        assert (await env.client.post(f"/sa/{SA_PATH}", json=payload, headers=headers)).status_code == 403
    assert len(env.sa.dp.updates) == 1  # ничего лишнего не дошло до суперадмин-бота


async def test_superadmin_secret_does_not_open_a_masters_bot_path_and_masters_path_does_not_open_superadmin(env):
    payload = message_update(1, 12345, "/start")
    response = await env.client.post(f"/tg/{BOT_ID[A]}/{SA_PATH}", json=payload, headers={SECRET_HEADER: SA_HEADER})
    assert response.status_code == 404
    assert env.tg.per_bot == {} and env.sa.dp.updates == []
