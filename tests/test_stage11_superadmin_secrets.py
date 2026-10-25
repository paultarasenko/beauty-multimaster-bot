"""Этап 11: суперадмин и секреты.

- действия суперадмина над мастером A (отключение, срок, смена бота) не меняют ни одной строки мастера B;
- токены хранятся только зашифрованными, не попадают в БД открытым текстом, в логи, журнал superadmin_actions, тексты
  ошибок и ответы бота; repr моделей и сводок без секретов;
- у мастера нет доступа к функциям суперадмина (middleware доступа; суперадмин-хендлеры не подключены к ботам мастеров).
"""

import datetime as dt
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from cryptography.fernet import Fernet
from sqlalchemy import delete, select

import app.superadmin.ui as superadmin_ui
from app.bot.dispatcher import default_routers
from app.bot.keyboards import callback_data as master_callbacks
from app.database.models import Base, Master, MasterBot, SuperadminAction
from app.database.session import make_session_factory
from app.multibot import management
from app.multibot.crypto import TokenCipher, TokenDecryptionError
from app.superadmin import service as sa
from app.superadmin.access import SuperadminAccessMiddleware
from app.superadmin.handlers import build_router as build_superadmin_router
from tests.conftest import TEST_DATABASE_URL
from tests.stage11_support import A, B, BOT_ID, OWNER, TOKEN, snapshot, two_masters  # noqa: F401  (фикстура pytest)
from tests.tg import RecordingSession
from tests.webhook_utils import FakeTelegram, message_update

SUPERADMIN_ID = 424242
NEW_TOKEN = "8300000003:" + "N" * 35
TODAY = dt.date(2030, 1, 15)


@pytest_asyncio.fixture
async def factory(session, two_masters):
    f = make_session_factory(TEST_DATABASE_URL)
    yield f
    async with f() as s:
        await s.execute(delete(SuperadminAction))
        await s.commit()
    await f.kw["bind"].dispose()


async def master_row(session, master_id: int) -> tuple:
    row = (await session.execute(select(Master).where(Master.id == master_id).execution_options(populate_existing=True))).scalar_one()
    return (row.id, row.owner_telegram_id, row.is_active, row.trial_ends_on, row.trial_expiring_notified_for, row.trial_expired_notified_on)


# ---------------------------------------------------------------- действия над A не меняют B


async def test_superadmin_actions_on_a_do_not_change_b(session, two_masters, factory):
    ns = two_masters
    before_rows, before_master = await snapshot(session, B), await master_row(session, B)

    assert await sa.set_master_active(factory, A, False) is True
    assert await sa.set_master_active(factory, A, True) is True
    assert await sa.set_bot_active(factory, A, False) == BOT_ID[A]
    assert await sa.set_bot_active(factory, A, True) == BOT_ID[A]
    assert await sa.set_trial_ends_on(factory, A, TODAY) is True
    assert await sa.extend_trial(factory, A, 30, TODAY) == TODAY + dt.timedelta(days=30)
    await sa.mark_digest_sent(factory, TODAY, [A], [A])

    # Смена бота у A: remove-bot + add-bot с новым токеном.
    tg = FakeTelegram()

    async def cleaner(bot_id: int) -> int:
        return 0

    make_bot = lambda token: Bot(token, session=tg)  # noqa: E731
    await management.remove_bot(factory, ns.cipher, master_id=A, bot_factory=make_bot, fsm_cleaner=cleaner)
    row = await management.add_bot(
        factory, ns.cipher, master_id=A, token=NEW_TOKEN, public_base_url="https://example.com", bot_factory=make_bot, fsm_cleaner=cleaner
    )
    assert row.master_id == A and row.telegram_bot_id == 8300000003

    session.expire_all()
    assert await snapshot(session, B) == before_rows  # включая master_bots B: токен, секрет, активность
    assert await master_row(session, B) == before_master  # активность, срок и служебные отметки B прежние


async def test_actions_on_missing_master_change_nothing(session, two_masters, factory):
    before = (await snapshot(session, A), await snapshot(session, B))
    assert await sa.set_master_active(factory, 999, False) is False
    assert await sa.set_bot_active(factory, 999, True) is None
    assert await sa.set_trial_ends_on(factory, 999, TODAY) is False
    assert await sa.extend_trial(factory, 999, 5, TODAY) is None
    assert (await snapshot(session, A), await snapshot(session, B)) == before


# ---------------------------------------------------------------- токены и секреты


async def test_tokens_are_stored_encrypted_and_never_in_plain_text_anywhere_in_db(session, two_masters, factory):
    ns = two_masters
    await sa.log_action(factory, actor_telegram_id=SUPERADMIN_ID, action="disable_master", master_id=A, bot_id=BOT_ID[A], detail="ok")
    rows = (await session.execute(select(MasterBot).order_by(MasterBot.master_id))).scalars().all()
    for row in rows:
        assert row.bot_token_encrypted not in TOKEN.values() and ":" not in row.bot_token_encrypted
        assert ns.cipher.decrypt(row.bot_token_encrypted) == TOKEN[row.master_id]  # а расшифровка ключом — настоящий токен
    secrets_ = {TOKEN[A], TOKEN[B], TOKEN[A].split(":")[1], TOKEN[B].split(":")[1]}
    for table in Base.metadata.sorted_tables:
        for record in (await session.execute(select(*table.c))).all():
            for cell in record:
                assert not any(secret in str(cell) for secret in secrets_), f"открытый токен в таблице {table.name}"


async def test_token_is_not_decryptable_with_another_key(two_masters):
    other = TokenCipher(Fernet.generate_key().decode())

    row_value = two_masters.cipher.encrypt(TOKEN[A])
    with pytest.raises(TokenDecryptionError) as error:
        other.decrypt(row_value)
    assert TOKEN[A] not in str(error.value) and row_value not in str(error.value)


async def test_models_and_summaries_do_not_expose_secrets(session, two_masters, factory):
    ns = two_masters
    texts = [repr(r) for r in (await session.execute(select(MasterBot))).scalars().all()]
    texts += [repr(r) for r in await sa.list_masters(factory)]
    texts += [repr(await sa.get_master_card(factory, m)) for m in (A, B)] + [repr(await sa.get_summary(factory))]
    await sa.log_action(factory, actor_telegram_id=SUPERADMIN_ID, action="x", master_id=A, detail=f"{TOKEN[A]} /tg/{BOT_ID[A]}/{ns.webhook_secret[A]}")
    texts += [repr(r) for r in await sa.recent_actions(factory)]
    joined = "\n".join(texts)
    for secret in (*TOKEN.values(), *(ns.webhook_secret.values()), *(TOKEN[m].split(":")[1] for m in (A, B))):
        assert secret not in joined
    assert str(BOT_ID[A]) in joined  # а id и username бота — показываются


async def test_journal_masks_tokens_and_webhook_paths(session, two_masters, factory):
    ns = two_masters
    detail = f"ошибка {TOKEN[A]} по адресу https://x.example/tg/{BOT_ID[A]}/{ns.webhook_secret[A]}"
    await sa.log_action(factory, actor_telegram_id=SUPERADMIN_ID, action="check", master_id=A, bot_id=BOT_ID[A], ok=False, detail=detail)
    stored = (await session.execute(select(SuperadminAction.detail))).scalar_one()
    assert TOKEN[A] not in stored and ns.webhook_secret[A] not in stored and TOKEN[A].split(":")[1] not in stored


async def test_errors_and_logs_of_bot_management_do_not_contain_tokens(session, two_masters, factory, caplog):
    ns = two_masters
    tg = FakeTelegram()
    make_bot = lambda token: Bot(token, session=tg)  # noqa: E731

    async def cleaner(bot_id: int) -> int:
        return 0

    await management.remove_bot(factory, ns.cipher, master_id=A, bot_factory=make_bot, fsm_cleaner=cleaner)
    tg.fail_set_webhook = True  # текст ошибки Telegram содержит токен бота (так делает и настоящий клиент)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(management.ManagementError) as error:
            await management.add_bot(
                factory, ns.cipher, master_id=A, token=NEW_TOKEN, public_base_url="https://example.com", bot_factory=make_bot, fsm_cleaner=cleaner
            )
        await sa.set_bot_active(factory, B, False)
    logs = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    for secret in (NEW_TOKEN, "N" * 35, TOKEN[B], TOKEN[A], *ns.webhook_secret.values()):
        assert secret not in str(error.value) and secret not in logs
    session.expire_all()
    assert (await session.execute(select(MasterBot.master_id))).scalars().all() == [B]  # неудачный add-bot ничего не оставил


async def test_bot_replies_to_master_do_not_contain_tokens_or_secrets(session, two_masters):
    from tests.tg import tg_harness

    ns = two_masters
    async with tg_harness(*default_routers(), master_id=A) as h:
        for text in ("/start", "🤖 Мой бот", "⚙️ Настройки", "👑 Профиль мастера"):
            await h.send_text(OWNER[A], text)
        shown = "\n".join(h.tg.everything_shown())
    for secret in (*TOKEN.values(), *ns.webhook_secret.values(), TOKEN[A].split(":")[1]):
        assert secret not in shown


# ---------------------------------------------------------------- мастер не суперадмин


async def test_access_middleware_lets_only_superadmin_through():
    calls = []

    async def handler(event, data):
        calls.append(data["event_from_user"].id)
        return "handled"

    middleware = SuperadminAccessMiddleware(frozenset({SUPERADMIN_ID}))
    assert await middleware(handler, object(), {"event_from_user": SimpleNamespace(id=SUPERADMIN_ID)}) == "handled"
    for outsider in (OWNER[A], OWNER[B], 1):
        assert await middleware(handler, object(), {"event_from_user": SimpleNamespace(id=outsider)}) is None
    assert await middleware(handler, object(), {}) is None  # апдейт без пользователя
    assert calls == [SUPERADMIN_ID]


async def test_masters_owner_gets_no_answer_from_superadmin_bot():
    tg = RecordingSession()
    bot = Bot("777000:" + "S" * 35, session=tg)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware.register(SuperadminAccessMiddleware(frozenset({SUPERADMIN_ID})))
    dp.include_router(build_superadmin_router())
    for owner in (OWNER[A], OWNER[B]):
        update = Update.model_validate(message_update(1, owner, "/start"), context={"bot": bot})
        await dp.feed_update(bot, update)
    assert tg.requests == []


def test_superadmin_handlers_are_not_attached_to_master_bots_and_callbacks_do_not_overlap():
    for router in default_routers():
        for observer in router.observers.values():
            for handler in observer.handlers:
                assert not handler.callback.__module__.startswith("app.superadmin"), handler.callback
    sa_prefixes = {cls.__prefix__ for cls in vars(superadmin_ui).values() if isinstance(cls, type) and hasattr(cls, "__prefix__") and cls.__module__ == superadmin_ui.__name__}
    master_prefixes = {cls.__prefix__ for cls in vars(master_callbacks).values() if isinstance(cls, type) and hasattr(cls, "__prefix__") and cls.__module__ == master_callbacks.__name__}
    assert sa_prefixes and master_prefixes and not (sa_prefixes & master_prefixes)
