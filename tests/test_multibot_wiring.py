"""Сборка Dispatcher, resolve_tenant в двух режимах, запуск webhook-режима без настроек, миграция master_bots."""

import asyncio

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.bot.dispatcher import build_dispatcher, default_routers
from app.config.settings import settings
from app.multibot.constants import ALLOWED_UPDATES
from app.multibot.crypto import TokenCipher
from app.multibot.registry import BotRegistry
from app.multibot.runner import run_webhook, validate_webhook_settings
from app.database.session import make_session_factory
from app.tenancy import TenantNotAvailableError, resolve_tenant
from tests.conftest import TEST_DATABASE_URL
from tests.test_migrations import _admin_dsn, _alembic, scratch_db_url  # noqa: F401  (фикстура)
from tests.test_multibot_registry import add_row
from tests.webhook_utils import FakeTelegram, token_for


def _detach():
    for router in default_routers():
        router._parent_router = None


def test_allowed_updates_match_what_dispatcher_uses():
    dp = build_dispatcher(make_session_factory(TEST_DATABASE_URL))
    try:
        assert set(dp.resolve_used_update_types()) == set(ALLOWED_UPDATES)
    finally:
        _detach()


async def test_polling_resolves_master_from_settings(session, monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "polling")
    tenant = await resolve_tenant(session, object(), {})
    assert tenant.master_id == settings.master_id


async def test_webhook_mode_without_registry_never_falls_back_to_master_1(session, monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "webhook")
    with pytest.raises(TenantNotAvailableError):
        await resolve_tenant(session, object(), {})


async def test_registry_resolves_master_by_bot_and_rejects_unknown_bot(session):
    cipher = TokenCipher(Fernet.generate_key().decode())
    await add_row(session, cipher, 1, 1001)
    tg = FakeTelegram()
    registry = BotRegistry(make_session_factory(TEST_DATABASE_URL), cipher, http_session_factory=lambda: tg)
    await registry.load_all()
    bot = (await registry.get(1001)).bot
    tenant = await resolve_tenant(session, object(), {"bot_registry": registry, "bot": bot})
    assert tenant.master_id == 1
    with pytest.raises(TenantNotAvailableError):
        await resolve_tenant(session, object(), {"bot_registry": registry, "bot": Bot(token=token_for(1001))})
    with pytest.raises(TenantNotAvailableError):
        await resolve_tenant(session, object(), {"bot_registry": registry})
    await registry.close()


def test_webhook_mode_does_not_start_without_key_or_https_url(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_encryption_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://x.example.com")
    with pytest.raises(SystemExit, match="BOT_TOKEN_ENCRYPTION_KEY"):
        asyncio.run(run_webhook(make_session_factory(TEST_DATABASE_URL)))
    monkeypatch.setattr(settings, "bot_token_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "public_base_url", "http://insecure.example.com")
    with pytest.raises(SystemExit, match="PUBLIC_BASE_URL"):
        asyncio.run(run_webhook(make_session_factory(TEST_DATABASE_URL)))
    monkeypatch.setattr(settings, "public_base_url", "https://ok.example.com")
    validate_webhook_settings()  # теперь без ошибок


def test_default_run_mode_is_polling():
    assert type(settings).model_fields["run_mode"].default == "polling"


# ---------- миграция master_bots ----------


def test_master_bots_migration_upgrade_downgrade_and_check(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", "s9d5e1f7a3b4").returncode == 0

    async def tables(conn):
        rows = await conn.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        return {r["table_name"] for r in rows}

    import asyncpg

    async def run(fn):
        conn = await asyncpg.connect(_admin_dsn(name))
        try:
            return await fn(conn)
        finally:
            await conn.close()

    assert "master_bots" not in asyncio.run(run(tables))
    assert _alembic(url, "upgrade", "t0e6f2a8b4c5").returncode == 0
    assert "master_bots" in asyncio.run(run(tables))

    async def constraints(conn):
        await conn.execute("INSERT INTO master_bots (master_id, telegram_bot_id, bot_token_encrypted, webhook_secret) VALUES (1, 10, 'x', 's1')")
        errors = []
        for sql in (
            "INSERT INTO master_bots (master_id, telegram_bot_id, bot_token_encrypted, webhook_secret) VALUES (1, 11, 'x', 's2')",  # второй бот мастеру
            "INSERT INTO master_bots (master_id, telegram_bot_id, bot_token_encrypted, webhook_secret) VALUES (2, 10, 'x', 's3')",  # тот же бот
            "INSERT INTO master_bots (master_id, telegram_bot_id, bot_token_encrypted, webhook_secret) VALUES (2, 12, 'x', 's1')",  # тот же секрет
        ):
            try:
                await conn.execute(sql)
                errors.append(None)
            except asyncpg.PostgresError as exc:
                errors.append(type(exc).__name__)
        await conn.execute("DELETE FROM masters WHERE id = 1")  # ON DELETE CASCADE
        return errors, await conn.fetchval("SELECT count(*) FROM master_bots")

    # мастер №2 для проверок уникальности
    async def prepare(conn):
        await conn.execute("INSERT INTO masters (id, is_active) VALUES (2, true)")

    asyncio.run(run(prepare))
    errors, left = asyncio.run(run(constraints))
    assert errors == ["UniqueViolationError"] * 3 and left == 0

    assert _alembic(url, "upgrade", "head").returncode == 0  # дальше появились миграции следующих этапов: check — на актуальной схеме
    assert _alembic(url, "check").returncode == 0
    assert _alembic(url, "downgrade", "s9d5e1f7a3b4").returncode == 0
    assert "master_bots" not in asyncio.run(run(tables))
    assert _alembic(url, "upgrade", "head").returncode == 0
