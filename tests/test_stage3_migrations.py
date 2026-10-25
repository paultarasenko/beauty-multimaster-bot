"""Миграция этапа 3: is_in_admin_panel переезжает users → master_clients с данными и обратно."""

import asyncio

import asyncpg

from tests.test_migrations import _admin_dsn, _alembic, scratch_db_url  # noqa: F401  (фикстура)

BEFORE_STAGE_3 = "r8c4d0e6f2a3"
STAGE_3 = "s9d5e1f7a3b4"


async def _run(db_name: str, fn):
    conn = await asyncpg.connect(_admin_dsn(db_name))
    try:
        return await fn(conn)
    finally:
        await conn.close()


async def _user_columns(conn) -> set[str]:
    rows = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
    return {r["column_name"] for r in rows}


def test_admin_panel_flag_is_copied_to_master_clients_and_back(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", BEFORE_STAGE_3).returncode == 0

    async def seed(conn):
        await conn.execute(
            "INSERT INTO users (telegram_id, is_in_admin_panel) VALUES (1, true), (2, false), (3, true)"
        )
        # Как на проде после этапа 1: у пользователей есть связь с мастером №1. Исключение — пользователь №3:
        # флаг есть, а строки master_clients нет (страховка миграции: режим меню тоже не должен потеряться).
        await conn.execute(
            "INSERT INTO master_clients (master_id, user_id) SELECT 1, id FROM users WHERE telegram_id IN (1, 2)"
        )

    asyncio.run(_run(name, seed))
    assert _alembic(url, "upgrade", STAGE_3).returncode == 0

    async def check_up(conn):
        rows = await conn.fetch(
            "SELECT u.telegram_id, mc.master_id, mc.is_in_admin_panel "
            "FROM users u JOIN master_clients mc ON mc.user_id = u.id ORDER BY u.telegram_id"
        )
        return [tuple(r.values()) for r in rows], await _user_columns(conn)

    rows, user_columns = asyncio.run(_run(name, check_up))
    assert rows == [(1, 1, True), (2, 1, False), (3, 1, True)]
    assert "is_in_admin_panel" not in user_columns  # из users колонка удалена

    assert _alembic(url, "downgrade", BEFORE_STAGE_3).returncode == 0

    async def check_down(conn):
        rows = await conn.fetch("SELECT telegram_id, is_in_admin_panel FROM users ORDER BY telegram_id")
        mc_cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'master_clients' AND column_name = 'is_in_admin_panel'"
        )
        return [tuple(r.values()) for r in rows], mc_cols

    users, mc_cols = asyncio.run(_run(name, check_down))
    assert users == [(1, True), (2, False), (3, True)]
    assert mc_cols == []  # в master_clients колонки больше нет


def test_flag_of_other_masters_is_not_copied_back_on_downgrade(scratch_db_url):
    """Откат возвращает «один мастер»: в users попадает только режим мастера №1, а не любого мастера."""
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", STAGE_3).returncode == 0

    async def seed(conn):
        await conn.execute("INSERT INTO masters (id, owner_telegram_id) VALUES (2, 777)")
        await conn.execute("INSERT INTO users (telegram_id) VALUES (10)")
        await conn.execute(
            "INSERT INTO master_clients (master_id, user_id, is_in_admin_panel) "
            "SELECT 1, id, false FROM users WHERE telegram_id = 10"
        )
        await conn.execute(
            "INSERT INTO master_clients (master_id, user_id, is_in_admin_panel) "
            "SELECT 2, id, true FROM users WHERE telegram_id = 10"
        )

    asyncio.run(_run(name, seed))
    assert _alembic(url, "downgrade", BEFORE_STAGE_3).returncode == 0

    async def check(conn):
        return await conn.fetchval("SELECT is_in_admin_panel FROM users WHERE telegram_id = 10")

    assert asyncio.run(_run(name, check)) is False
