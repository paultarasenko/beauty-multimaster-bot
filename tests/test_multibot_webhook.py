"""Webhook-endpoint: проверка секретов, игнор неактивных, сквозная изоляция двух ботов двух мастеров."""

import datetime as dt
import logging

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from app.bot.dispatcher import build_dispatcher, default_routers
from app.bot.disabled_replies import CLIENT_TEXT as DISABLED_CLIENT_TEXT
from app.database.models import Master, MasterBot, MasterClient, Service, User, WorkingHours
from app.database.session import make_session_factory
from app.multibot.crypto import TokenCipher
from app.multibot.registry import BotRegistry
from app.multibot.webhook import SECRET_HEADER, create_webhook_app
from app.services.access_request_service import BLOCKED_TEXT
from app.services.booking_service import BookingService
from tests.conftest import RIGA_TZ, MASTER_ID, make_client, next_weekday, set_client, set_owner
from tests.test_multibot_registry import add_master, add_row
from tests.webhook_utils import FakeTelegram, message_update

A, B = 1, 2
BOT_A, BOT_B = 1001, 1002
OWNER_A, OWNER_B = 900, 800
CLIENT = 5001
SERVICES_BTN = "💰 Услуги и цены"
MY_BOOKINGS_BTN = "📋 Мои записи"
CONTACT_BTN = "📞 Связаться со мной"


class Env:
    def __init__(self, cipher, tg, registry, client, rows):
        self.cipher, self.tg, self.registry, self.client, self.rows = cipher, tg, registry, client, rows
        self._uid = 0

    async def post(self, bot_id, secret, payload, *, header="valid", raw: bytes | None = None, headers=None):
        row = next((r for r in self.rows.values() if r.telegram_bot_id == bot_id), None)
        hdrs = dict(headers or {})
        if header == "valid" and row is not None:
            hdrs[SECRET_HEADER] = self.cipher.header_token(row.webhook_secret)
        elif header not in ("valid", None):
            hdrs[SECRET_HEADER] = header
        return await self.client.post(f"/tg/{bot_id}/{secret}", json=None if raw is not None else payload, content=raw, headers=hdrs)

    async def send(self, bot: int, user_id: int, text: str) -> httpx.Response:
        """Сообщение пользователя боту: с правильным путём и заголовком."""
        self._uid += 1
        row = self.rows[bot]
        return await self.post(bot, row.webhook_secret, message_update(self._uid, user_id, text))


@pytest_asyncio.fixture
async def env(session):
    cipher = TokenCipher(Fernet.generate_key().decode())
    await add_master(session, B, OWNER_B)
    await set_owner(session, OWNER_A, A)
    rows = {BOT_A: await add_row(session, cipher, A, BOT_A), BOT_B: await add_row(session, cipher, B, BOT_B)}
    tg = FakeTelegram()
    factory = make_session_factory(__import__("tests.conftest", fromlist=["x"]).TEST_DATABASE_URL)
    registry = BotRegistry(factory, cipher, http_session_factory=lambda: tg)
    await registry.load_all()
    dp = build_dispatcher(factory, workflow_data={"bot_registry": registry})
    app = create_webhook_app(registry, dp, max_body_bytes=100_000)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        yield Env(cipher, tg, registry, client, rows)
    finally:
        await client.aclose()
        await registry.close()
        for router in default_routers():
            router._parent_router = None  # модульные роутеры подключаются к диспетчеру один раз


# ---------- защита endpoint ----------


async def test_health(env):
    response = await env.client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_valid_secret_processes_update(env, session):
    response = await env.send(BOT_A, CLIENT, "/start")
    assert response.status_code == 200
    assert env.tg.texts_of(BOT_A)  # бот A ответил


async def test_wrong_path_secret_and_unknown_bot_are_404_without_details(env):
    payload = message_update(1, CLIENT, "/start")
    for bot_id, secret in [(BOT_A, "wrong"), (999999, "whatever"), (BOT_A, env.rows[BOT_B].webhook_secret)]:
        response = await env.post(bot_id, secret, payload)
        assert response.status_code == 404 and response.content == b""
    response = await env.client.post("/tg/not-a-number/x", json=payload)
    assert response.status_code == 404 and response.content == b""
    assert env.tg.per_bot == {}  # ни один бот ничего не обработал


async def test_wrong_or_missing_header_token_is_403(env):
    secret = env.rows[BOT_A].webhook_secret
    payload = message_update(1, CLIENT, "/start")
    assert (await env.post(BOT_A, secret, payload, header="bad-token")).status_code == 403
    assert (await env.post(BOT_A, secret, payload, header=None)).status_code == 403
    # путь верный, но заголовок от бота B — тоже отказ
    other = env.cipher.header_token(env.rows[BOT_B].webhook_secret)
    assert (await env.post(BOT_A, secret, payload, header=other)).status_code == 403
    assert env.tg.per_bot == {}


async def test_path_secret_alone_is_not_enough_header_is_not_the_path_secret(env):
    secret = env.rows[BOT_A].webhook_secret
    response = await env.post(BOT_A, secret, message_update(1, CLIENT, "/start"), header=secret)
    assert response.status_code == 403


async def test_oversized_body_is_413(env):
    secret = env.rows[BOT_A].webhook_secret
    response = await env.post(BOT_A, secret, None, raw=b"{" + b" " * 200_000 + b"}")
    assert response.status_code == 413
    assert env.tg.per_bot == {}


async def test_inactive_bot_is_ignored_with_200(env, session):
    await session.execute(update(MasterBot).where(MasterBot.telegram_bot_id == BOT_A).values(is_active=False))
    await session.commit()
    await env.registry.refresh(BOT_A)
    response = await env.send(BOT_A, CLIENT, "/start")
    assert response.status_code == 200
    assert env.tg.texts_of(BOT_A) == []
    assert (await session.execute(select(User).where(User.telegram_id == CLIENT))).scalar_one_or_none() is None


async def test_inactive_master_is_ignored_with_200(env, session):
    await session.execute(update(Master).where(Master.id == B).values(is_active=False))
    await session.commit()
    await env.registry.refresh(BOT_B)
    response = await env.send(BOT_B, CLIENT, "/start")
    assert response.status_code == 200 and env.tg.texts_of(BOT_B) == []
    # бот A при этом работает
    await env.send(BOT_A, CLIENT, "/start")
    assert env.tg.texts_of(BOT_A)


async def test_master_deactivated_after_cache_is_dropped_by_tenant_middleware(env, session):
    """Реестр ещё считает мастера активным, но TenantMiddleware видит свежее состояние БД — апдейт до хендлеров
    не доходит (клиент не создаётся), ответ 200; клиент получает только вежливую фразу про отключённого мастера (этап 10)."""
    await session.execute(update(Master).where(Master.id == B).values(is_active=False))
    await session.commit()
    response = await env.send(BOT_B, CLIENT, "/start")  # кэш реестра не обновляли
    assert response.status_code == 200
    assert env.tg.texts_of(BOT_B) == [DISABLED_CLIENT_TEXT]
    assert (await session.execute(select(User).where(User.telegram_id == CLIENT))).scalar_one_or_none() is None


async def test_garbage_body_after_auth_is_200_and_not_processed(env):
    secret = env.rows[BOT_A].webhook_secret
    for raw in (b"not json", b'{"no_update_id": true}', b"[]"):
        response = await env.post(BOT_A, secret, None, raw=raw)
        assert response.status_code == 200
    assert env.tg.per_bot == {}


async def test_handler_crash_is_logged_and_still_200(env, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("dispatcher exploded")

    from aiogram import Dispatcher

    monkeypatch.setattr(Dispatcher, "feed_update", boom)
    with caplog.at_level(logging.ERROR):
        response = await env.send(BOT_A, CLIENT, "/start")
    assert response.status_code == 200
    assert any("Ошибка обработки апдейта" in r.getMessage() for r in caplog.records)


async def test_token_and_secret_never_appear_in_logs(env, caplog):
    with caplog.at_level(logging.DEBUG):
        await env.send(BOT_A, CLIENT, "/start")
        await env.post(BOT_A, "wrong", message_update(2, CLIENT, "/start"))
    # httpx — логгер самого тестового клиента (URL запроса), к серверу не относится.
    log = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert log  # сервер что-то логировал
    assert env.rows[BOT_A].webhook_secret not in log
    from tests.webhook_utils import token_for

    assert token_for(BOT_A) not in log and token_for(BOT_B) not in log


# ---------- изоляция мастеров через два бота ----------


async def _service(session, master_id: int, name: str) -> Service:
    s = Service(master_id=master_id, name=name, price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(s)
    await session.commit()
    return s


async def test_update_is_handled_in_context_of_the_bots_master(env, session):
    await _service(session, A, "Маникюр-мастера-А")
    await _service(session, B, "Педикюр-мастера-Б")
    await env.send(BOT_A, CLIENT, SERVICES_BTN)
    await env.send(BOT_B, CLIENT, SERVICES_BTN)

    shown_a, shown_b = "\n".join(env.tg.texts_of(BOT_A)), "\n".join(env.tg.texts_of(BOT_B))
    assert "Маникюр-мастера-А" in shown_a and "Педикюр-мастера-Б" not in shown_a
    assert "Педикюр-мастера-Б" in shown_b and "Маникюр-мастера-А" not in shown_b


async def test_client_is_registered_separately_per_master(env, session):
    await env.send(BOT_A, CLIENT, "/start")
    links = (await session.execute(select(MasterClient.master_id))).scalars().all()
    assert links == [A]  # у мастера Б этого клиента ещё нет
    await env.send(BOT_B, CLIENT, "/start")
    links = (await session.execute(select(MasterClient.master_id).order_by(MasterClient.master_id))).scalars().all()
    assert links == [A, B]
    assert len((await session.execute(select(User).where(User.telegram_id == CLIENT))).scalars().all()) == 1  # один человек


async def test_booking_made_via_bot_a_is_invisible_in_bot_b(env, session):
    day = next_weekday(4)
    session.add(WorkingHours(master_id=A, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    service = await _service(session, A, "Маникюр-мастера-А")
    user = await make_client(session, CLIENT, A, first_name="Ivan")
    await session.commit()
    await BookingService(session, RIGA_TZ, master_id=A).create_booking(
        user.id, service.id, dt.datetime.combine(day, dt.time(12), tzinfo=RIGA_TZ)
    )
    await session.commit()

    await env.send(BOT_A, CLIENT, MY_BOOKINGS_BTN)
    await env.send(BOT_B, CLIENT, MY_BOOKINGS_BTN)
    assert any("Маникюр-мастера-А" in t for t in env.tg.texts_of(BOT_A))
    assert not any("Маникюр-мастера-А" in t for t in env.tg.texts_of(BOT_B))
    assert any("нет активных записей" in t or "нет записей" in t for t in env.tg.texts_of(BOT_B))


async def test_owner_of_a_is_not_admin_in_bot_b(env, session):
    """Владелец A: в боте A видит вход в админ-панель и попадает в неё, в боте B — обычный клиент."""
    from aiogram.methods import SendMessage

    from app.bot.keyboards.admin_keyboards import admin_menu_keyboard

    ADMIN_ENTRY = "👑 Профиль мастера"

    def keyboards_of(bot_id):
        return [m.reply_markup for m in env.tg.methods_of(bot_id, SendMessage) if getattr(m.reply_markup, "keyboard", None)]

    def labels(markup):
        return {b.text for row in markup.keyboard for b in row}

    from app.bot.keyboards.client_keyboards import main_menu_keyboard

    # кнопки, которые есть только у админ-меню (общие с клиентским — «Услуги и цены» и т.п. — не считаем)
    admin_only = labels(admin_menu_keyboard()) - labels(main_menu_keyboard(False)) - labels(main_menu_keyboard(True)) - {ADMIN_ENTRY}
    assert admin_only

    await env.send(BOT_A, OWNER_A, "/start")
    await env.send(BOT_B, OWNER_A, "/start")
    assert ADMIN_ENTRY in set().union(*(labels(k) for k in keyboards_of(BOT_A)))
    assert ADMIN_ENTRY not in set().union(*(labels(k) for k in keyboards_of(BOT_B)))

    await env.send(BOT_A, OWNER_A, ADMIN_ENTRY)
    await env.send(BOT_B, OWNER_A, ADMIN_ENTRY)  # в боте B это не админ — кнопка не открывает админ-панель
    # админ-меню (его уникальные кнопки) показано только ботом A
    assert any(admin_only <= labels(k) for k in keyboards_of(BOT_A))
    assert not any(admin_only & labels(k) for k in keyboards_of(BOT_B))

    rows = {
        r.master_id: r.is_in_admin_panel
        for r in (await session.execute(select(MasterClient).join(User, User.id == MasterClient.user_id).where(User.telegram_id == OWNER_A))).scalars()
    }
    assert rows.get(A) is True and rows.get(B) in (None, False)


async def test_same_telegram_user_has_independent_restrictions_in_two_bots(env, session):
    await make_client(session, CLIENT, A)
    await make_client(session, CLIENT, B)
    await session.commit()
    await set_client(session, (await session.execute(select(User.id).where(User.telegram_id == CLIENT))).scalar_one(), A, is_blocked_forever=True)

    await env.send(BOT_A, CLIENT, CONTACT_BTN)
    await env.send(BOT_B, CLIENT, CONTACT_BTN)
    assert BLOCKED_TEXT in env.tg.texts_of(BOT_A)  # у мастера А клиент заблокирован
    assert BLOCKED_TEXT not in env.tg.texts_of(BOT_B)  # у мастера Б — свободен
    assert env.tg.texts_of(BOT_B)  # и бот Б ему отвечает
