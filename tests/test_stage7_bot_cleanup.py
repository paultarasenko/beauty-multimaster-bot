"""Этап 7: remove-bot / add-bot чистят состояния FSM бота, чтобы чужие диалоги не перешли новому мастеру."""

import pytest
from aiogram import Bot
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import SetWebhook
from cryptography.fernet import Fernet
from sqlalchemy import select

from app import cli
from app.bot.fsm_storage import FsmStorageError, clear_bot_states, create_fsm_backend
from app.config.settings import settings
from app.database.models import MasterBot
from app.database.session import make_session_factory
from app.multibot import management
from app.multibot.crypto import TokenCipher
from tests.conftest import TEST_DATABASE_URL
from tests.test_multibot_registry import add_master
from tests.webhook_utils import FakeTelegram, token_for

BASE = "https://bot.example.com"
BOT_ID = 555001
TOKEN = token_for(BOT_ID)


@pytest.fixture
def cipher(monkeypatch) -> TokenCipher:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "bot_token_encryption_key", key)
    monkeypatch.setattr(settings, "public_base_url", BASE)
    return TokenCipher(key)


@pytest.fixture
async def factory():
    factory = make_session_factory(TEST_DATABASE_URL)
    yield factory
    await factory.kw["bind"].dispose()


def key(bot_id: int, user: int = 1) -> StorageKey:
    return StorageKey(bot_id=bot_id, chat_id=user, user_id=user)


class Cleaner:
    """Подменяет очиститель CLI: чистит Redis под тестовым префиксом и запоминает вызовы (и состояние БД в момент вызова)."""

    def __init__(self, storage, factory):
        self.storage, self.factory, self.calls, self.rows_at_call = storage, factory, [], []
        self.fail = False
        self.memory_mode = False

    async def __call__(self, bot_id: int):
        self.calls.append(bot_id)
        async with self.factory() as session:
            self.rows_at_call.append((await session.execute(select(MasterBot.id).where(MasterBot.telegram_bot_id == bot_id))).first() is not None)
        if self.fail:
            raise FsmStorageError("Redis недоступен (test)")
        return None if self.memory_mode else await clear_bot_states(self.storage, bot_id)


@pytest.fixture
async def world(session, redis_url, fsm_prefix, factory):
    backend = create_fsm_backend(kind="redis", redis_url=redis_url, prefix=fsm_prefix)
    await add_master(session, 2, 777)
    yield Cleaner(backend.storage, factory), backend.storage
    await backend.storage.close()


async def _seed(storage, bot_id, users=(1, 2)):
    for user in users:
        await storage.set_state(key(bot_id, user), "BookingFSM:select_date")
        await storage.set_data(key(bot_id, user), {"service_id": 1})


async def _add(cipher, factory, tg, cleaner, master_id):
    return await management.add_bot(
        factory, cipher, master_id=master_id, token=TOKEN, public_base_url=BASE,
        bot_factory=lambda t: Bot(token=t, session=tg), fsm_cleaner=cleaner,
    )


async def _remove(cipher, factory, tg, cleaner, master_id):
    return await management.remove_bot(
        factory, cipher, master_id=master_id, bot_factory=lambda t: Bot(token=t, session=tg), fsm_cleaner=cleaner
    )


async def test_remove_bot_clears_that_bots_states_only(world, cipher, factory):
    cleaner, storage = world
    tg = FakeTelegram()
    await _add(cipher, factory, tg, cleaner, 1)
    await _seed(storage, BOT_ID)
    await _seed(storage, 999)  # другой бот другого мастера
    assert await _remove(cipher, factory, tg, cleaner, 1) is None
    assert await storage.get_state(key(BOT_ID, 1)) is None and await storage.get_data(key(BOT_ID, 2)) == {}
    assert await storage.get_state(key(999, 1)) == "BookingFSM:select_date"
    assert cleaner.rows_at_call[-1] is False  # очистка — после удаления строки master_bots


async def test_same_bot_moved_to_another_master_does_not_inherit_old_states(world, cipher, factory, session):
    cleaner, storage = world
    tg = FakeTelegram()
    await _add(cipher, factory, tg, cleaner, 1)
    await _seed(storage, BOT_ID)
    await _remove(cipher, factory, tg, cleaner, 1)
    await _seed(storage, BOT_ID)  # «просочилось» после очистки (запоздавший апдейт) — страховка add-bot
    await _add(cipher, factory, tg, cleaner, 2)
    assert await storage.get_state(key(BOT_ID, 1)) is None and await storage.get_data(key(BOT_ID, 1)) == {}
    row = (await session.execute(select(MasterBot))).scalar_one()
    assert row.master_id == 2
    assert cleaner.rows_at_call[-1] is False  # add-bot очищает ДО появления бота у нового мастера


async def test_add_bot_aborts_when_old_states_cannot_be_cleared(world, cipher, factory, session):
    cleaner, storage = world
    cleaner.fail = True
    tg = FakeTelegram()
    with pytest.raises(management.ManagementError, match="не подключён"):
        await _add(cipher, factory, tg, cleaner, 1)
    assert (await session.execute(select(MasterBot))).first() is None  # бот не подключён
    assert not tg.methods_of(BOT_ID, SetWebhook)  # и webhook не установлен


async def test_remove_bot_with_unavailable_redis_still_removes_and_warns(world, cipher, factory, session):
    cleaner, _ = world
    tg = FakeTelegram()
    await _add(cipher, factory, tg, cleaner, 1)
    cleaner.fail = True
    warning = await _remove(cipher, factory, tg, cleaner, 1)
    assert warning and "не очищены" in warning and "Redis" in warning
    assert (await session.execute(select(MasterBot))).first() is None


async def test_remove_bot_in_memory_mode_explains_how_states_disappear(world, cipher, factory):
    cleaner, _ = world
    tg = FakeTelegram()
    await _add(cipher, factory, tg, cleaner, 1)
    cleaner.memory_mode = True
    warning = await _remove(cipher, factory, tg, cleaner, 1)
    assert warning and "памяти" in warning


async def test_cli_passes_cleaner_through(world, cipher, factory):
    cleaner, storage = world
    tg = FakeTelegram()
    out: list[str] = []

    def run(argv):
        return cli.run(
            cli.build_parser().parse_args(argv), session_factory=factory,
            bot_factory=lambda t: Bot(token=t, session=tg), fsm_cleaner=cleaner, out=out.append,
        )

    assert await run(["add-bot", "--master-id", "1", "--token", TOKEN]) == 0
    await _seed(storage, BOT_ID)
    assert await run(["remove-bot", "--master-id", "1"]) == 0
    assert await storage.get_state(key(BOT_ID, 1)) is None
    assert BOT_ID in cleaner.calls and TOKEN not in "\n".join(out)
