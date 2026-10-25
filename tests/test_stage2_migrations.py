"""Миграции этапа 2: клиентские флаги переезжают users → master_clients с данными, default master_id снимается."""

import asyncio

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL
from tests.test_migrations import _admin_dsn, _alembic, scratch_db_url  # noqa: F401  (фикстура)

BEFORE_STAGE_2 = "p6a2b8c4d0e1"

TENANT_TABLES = [
    "services", "working_hours", "breaks", "blocked_periods", "bookings", "reviews", "portfolio_items",
    "notifications", "admin_reminders", "client_access_requests", "booking_limit_requests", "master_profile",
    "master_clients",
]


async def _run(db_name: str, fn):
    conn = await asyncpg.connect(_admin_dsn(db_name))
    try:
        return await fn(conn)
    finally:
        await conn.close()


def test_flags_are_copied_to_master_clients_and_back(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", BEFORE_STAGE_2).returncode == 0

    async def seed(conn):
        await conn.execute(
            "INSERT INTO users (telegram_id, is_restricted, is_blocked_forever, over_limit_cancels, cancel_streak_reset_at) "
            "VALUES (1, true, false, 2, now()), (2, true, true, NULL, NULL), (3, false, false, NULL, NULL)"
        )
        # пользователь без строки master_clients (страховка миграции): тоже не должен потерять флаги
        await conn.execute("DELETE FROM master_clients WHERE user_id = (SELECT id FROM users WHERE telegram_id = 2)")

    asyncio.run(_run(name, seed))
    assert _alembic(url, "upgrade", "q7b3c9d5e1f2").returncode == 0

    async def check_up(conn):
        rows = await conn.fetch(
            "SELECT u.telegram_id, mc.master_id, mc.is_restricted, mc.is_blocked_forever, mc.over_limit_cancels, "
            "mc.cancel_streak_reset_at IS NOT NULL AS has_reset "
            "FROM users u JOIN master_clients mc ON mc.user_id = u.id ORDER BY u.telegram_id"
        )
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users' AND column_name = ANY($1)",
            ["is_restricted", "is_blocked_forever", "over_limit_cancels", "cancel_streak_reset_at"],
        )
        return [tuple(r.values()) for r in rows], cols

    rows, leftover = asyncio.run(_run(name, check_up))
    assert rows == [
        (1, 1, True, False, 2, True),
        (2, 1, True, True, None, False),
        (3, 1, False, False, None, False),
    ]
    assert leftover == []  # колонок в users больше нет

    assert _alembic(url, "downgrade", BEFORE_STAGE_2).returncode == 0

    async def check_down(conn):
        return [
            tuple(r.values())
            for r in await conn.fetch(
                "SELECT telegram_id, is_restricted, is_blocked_forever, over_limit_cancels FROM users ORDER BY telegram_id"
            )
        ]

    assert asyncio.run(_run(name, check_down)) == [(1, True, False, 2), (2, True, True, None), (3, False, False, None)]


def test_master_id_default_is_dropped_and_restored_on_downgrade(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", "head").returncode == 0

    async def defaults(conn):
        rows = await conn.fetch(
            "SELECT table_name, column_default FROM information_schema.columns "
            "WHERE column_name = 'master_id' AND table_schema = 'public'"
        )
        return {r["table_name"]: r["column_default"] for r in rows}

    result = asyncio.run(_run(name, defaults))
    # master_bots (этап 4) — реестр ботов мастеров, а не данные мастера: переходного default у неё не было.
    result.pop("master_bots", None)
    result.pop("master_settings", None)  # этап 8: настройки мастера, переходного default у них не было
    result.pop("working_day_overrides", None)  # особые дни: таблица появилась позже, переходного default у неё не было
    result.pop("master_quiet_mode", None)  # тихий режим: таблицы появились позже, переходного default у них не было
    result.pop("master_deferred_messages", None)
    assert set(result) == set(TENANT_TABLES)
    assert all(default is None for default in result.values()), result

    assert _alembic(url, "downgrade", "q7b3c9d5e1f2").returncode == 0
    restored = asyncio.run(_run(name, defaults))
    restored.pop("master_bots", None)
    restored.pop("master_settings", None)
    restored.pop("working_day_overrides", None)
    restored.pop("master_quiet_mode", None)
    restored.pop("master_deferred_messages", None)
    assert all(default == "1" for default in restored.values()), restored
