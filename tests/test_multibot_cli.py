"""CLI управления ботами (python -m app.cli) с замоканным Telegram."""

import argparse

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.methods import DeleteWebhook, GetMe, SetWebhook
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import cli
from app.config.settings import settings
from app.database.models import Master, MasterBot
from app.database.session import make_session_factory
from app.multibot import management
from app.multibot.constants import ALLOWED_UPDATES
from app.multibot.crypto import ConfigurationError, TokenCipher
from tests.conftest import TEST_DATABASE_URL
from tests.test_multibot_registry import add_master
from tests.webhook_utils import FakeTelegram, token_for

BASE = "https://bot.example.com"
TOKEN = token_for(555001)


@pytest.fixture
def key(monkeypatch) -> str:
    k = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "bot_token_encryption_key", k)
    monkeypatch.setattr(settings, "public_base_url", BASE)
    return k


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def factory():
    return make_session_factory(TEST_DATABASE_URL)


def run_cli(argv, factory, tg, out):
    args = cli.build_parser().parse_args(argv)
    return cli.run(args, session_factory=factory, bot_factory=lambda token: Bot(token=token, session=tg), out=out.append)


async def test_add_bot_validates_token_encrypts_and_sets_webhook(session, key, tg, factory):
    out: list[str] = []
    assert await run_cli(["add-bot", "--master-id", "1", "--token", TOKEN], factory, tg, out) == 0

    row = (await session.execute(select(MasterBot))).scalar_one()
    assert (row.master_id, row.telegram_bot_id, row.bot_username, row.is_active) == (1, 555001, "bot555001_bot", True)
    assert TOKEN not in row.bot_token_encrypted
    assert TokenCipher(key).decrypt(row.bot_token_encrypted) == TOKEN
    assert len(row.webhook_secret) >= 40

    assert len(tg.methods_of(555001, GetMe)) == 1
    (hook,) = tg.methods_of(555001, SetWebhook)
    assert hook.url == f"{BASE}/tg/555001/{row.webhook_secret}"
    assert hook.secret_token == TokenCipher(key).header_token(row.webhook_secret)
    assert hook.allowed_updates == ALLOWED_UPDATES
    # ни токен, ни секреты не напечатаны
    printed = "\n".join(out)
    assert TOKEN not in printed and row.webhook_secret not in printed and hook.secret_token not in printed


async def test_add_bot_second_bot_for_master_and_same_bot_for_two_masters_are_rejected(session, key, tg, factory):
    await add_master(session, 2, 777)
    cipher = TokenCipher(key)
    await management.add_bot(factory, cipher, master_id=1, token=TOKEN, public_base_url=BASE, bot_factory=lambda t: Bot(token=t, session=tg))
    with pytest.raises(management.ManagementError, match="уже есть бот"):
        await management.add_bot(factory, cipher, master_id=1, token=token_for(555002), public_base_url=BASE, bot_factory=lambda t: Bot(token=t, session=tg))
    with pytest.raises(management.ManagementError, match="уже подключён к мастеру id=1"):
        await management.add_bot(factory, cipher, master_id=2, token=TOKEN, public_base_url=BASE, bot_factory=lambda t: Bot(token=t, session=tg))
    assert len((await session.execute(select(MasterBot))).scalars().all()) == 1


async def test_add_bot_unknown_master_and_bad_base_url(session, key, tg, factory):
    cipher = TokenCipher(key)
    mk = lambda t: Bot(token=t, session=tg)  # noqa: E731
    with pytest.raises(management.ManagementError, match="не найден"):
        await management.add_bot(factory, cipher, master_id=99, token=TOKEN, public_base_url=BASE, bot_factory=mk)
    for bad in ("", "http://bot.example.com", "bot.example.com"):
        with pytest.raises(ConfigurationError, match="PUBLIC_BASE_URL"):
            await management.add_bot(factory, cipher, master_id=1, token=TOKEN, public_base_url=bad, bot_factory=mk)
    assert tg.per_bot == {}  # до Telegram дело не дошло


async def test_add_bot_rolls_back_row_when_set_webhook_fails_and_hides_token(session, key, tg, factory):
    tg.fail_set_webhook = True
    with pytest.raises(management.ManagementError) as exc:
        await management.add_bot(factory, TokenCipher(key), master_id=1, token=TOKEN, public_base_url=BASE, bot_factory=lambda t: Bot(token=t, session=tg))
    assert TOKEN not in str(exc.value)
    assert (await session.execute(select(MasterBot))).scalars().all() == []


def test_cli_main_reports_error_without_token(_test_database, key, monkeypatch, capsys):
    """Полный путь main(): ошибка Telegram → код 1, токена нет ни в stdout, ни в stderr."""
    tg = FakeTelegram()
    tg.fail_set_webhook = True
    monkeypatch.setattr(management, "default_bot_factory", lambda t: Bot(token=t, session=tg))
    # NullPool: каждый cli.main() идёт в своём asyncio.run, соединения пула нельзя переносить между циклами событий.
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    monkeypatch.setattr(cli, "async_session_factory", async_sessionmaker(engine, expire_on_commit=False))

    assert cli.main(["add-bot", "--master-id", "1", "--token", TOKEN]) == 1
    captured = capsys.readouterr()
    assert "Ошибка" in captured.err and TOKEN not in captured.out + captured.err

    assert cli.main(["list-bots"]) == 0  # и вывод без токенов/секретов; запись откатилась, бота нет
    assert "бота нет" in capsys.readouterr().out


async def test_remove_bot_deletes_webhook_and_row_but_keeps_master(session, key, tg, factory):
    cipher = TokenCipher(key)
    mk = lambda t: Bot(token=t, session=tg)  # noqa: E731
    await management.add_bot(factory, cipher, master_id=1, token=TOKEN, public_base_url=BASE, bot_factory=mk)
    out: list[str] = []
    assert await run_cli(["remove-bot", "--master-id", "1"], factory, tg, out) == 0
    assert (await session.execute(select(MasterBot))).scalars().all() == []
    assert tg.methods_of(555001, DeleteWebhook)
    assert await session.get(Master, 1) is not None
    with pytest.raises(management.ManagementError, match="нет подключённого бота"):
        await management.remove_bot(factory, cipher, master_id=1, bot_factory=mk)


async def test_refresh_webhook_reuses_or_rotates_secret(session, key, tg, factory):
    cipher = TokenCipher(key)
    mk = lambda t: Bot(token=t, session=tg)  # noqa: E731
    await management.add_bot(factory, cipher, master_id=1, token=TOKEN, public_base_url=BASE, bot_factory=mk)
    secret = (await session.execute(select(MasterBot.webhook_secret))).scalar_one()

    await management.refresh_webhook(factory, cipher, master_id=1, public_base_url="https://new.example.com", bot_factory=mk)
    assert tg.methods_of(555001, SetWebhook)[-1].url == f"https://new.example.com/tg/555001/{secret}"

    await management.refresh_webhook(factory, cipher, master_id=1, public_base_url=BASE, rotate_secret=True, bot_factory=mk)
    session.expire_all()
    new_secret = (await session.execute(select(MasterBot.webhook_secret))).scalar_one()
    assert new_secret != secret
    last = tg.methods_of(555001, SetWebhook)[-1]
    assert last.url == f"{BASE}/tg/555001/{new_secret}" and last.secret_token == cipher.header_token(new_secret)


async def test_create_master_and_list_bots_without_secrets(session, key, tg, factory):
    out: list[str] = []
    await run_cli(["create-master", "--owner-telegram-id", "4242"], factory, tg, out)
    assert "id=" in out[0]
    await run_cli(["add-bot", "--master-id", "1", "--token", TOKEN], factory, tg, out)
    out.clear()
    await run_cli(["list-bots"], factory, tg, out)
    text = "\n".join(out)
    assert "master_id=1" in text and "@bot555001_bot" in text and "бота нет" in text and "4242" in text
    row = (await session.execute(select(MasterBot))).scalar_one()
    assert TOKEN not in text and row.webhook_secret not in text and row.bot_token_encrypted not in text


async def test_add_bot_without_encryption_key_fails_clearly(session, monkeypatch, tg, factory):
    monkeypatch.setattr(settings, "bot_token_encryption_key", "")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with pytest.raises(ConfigurationError, match="BOT_TOKEN_ENCRYPTION_KEY"):
        await run_cli(["add-bot", "--master-id", "1", "--token", TOKEN], factory, tg, [])
