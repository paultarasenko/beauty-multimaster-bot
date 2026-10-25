"""Миграция этапа 8: master_settings для существующих мастеров, недостающие профили, откат."""

import asyncio

import asyncpg

from tests.test_migrations import _admin_dsn, _alembic, scratch_db_url  # noqa: F401  (фикстура)

BEFORE_STAGE_8 = "u1f7a3b9c5d6"
STAGE_8 = "v2a8b4c0d6e7"


async def _run(db_name: str, fn):
    conn = await asyncpg.connect(_admin_dsn(db_name))
    try:
        return await fn(conn)
    finally:
        await conn.close()


def test_existing_masters_get_default_settings_and_missing_profiles(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", BEFORE_STAGE_8).returncode == 0

    async def seed(conn):
        # мастер №1 уже есть (создан миграциями); второй — без профиля, у первого профиль со своим часовым поясом
        await conn.execute("INSERT INTO masters (id, owner_telegram_id, is_active) VALUES (2, 777, true)")
        await conn.execute("INSERT INTO master_profile (master_id, name, timezone) VALUES (1, 'Мария', 'Europe/Riga')")

    asyncio.run(_run(name, seed))
    assert _alembic(url, "upgrade", STAGE_8).returncode == 0

    async def check(conn):
        settings = await conn.fetch("SELECT * FROM master_settings ORDER BY master_id")
        profiles = await conn.fetch("SELECT master_id, name, timezone FROM master_profile ORDER BY master_id")
        return settings, profiles

    settings, profiles = asyncio.run(_run(name, check))
    assert [s["master_id"] for s in settings] == [1, 2]
    for row in settings:  # значения по умолчанию = прежнее поведение
        assert (row["slot_interval_minutes"], row["max_active_bookings"], row["cancel_streak_limit"], row["currency"]) == (30, 3, 3, "BYN")
        assert all(row[f] is True for f in ("reminder_24h_enabled", "reminder_2h_enabled", "master_reminder_2h_enabled", "allow_reschedule", "allow_reviews"))
        assert all(row[f] is None for f in ("booking_confirmation_text", "cancellation_text", "reminder_24h_text", "reminder_2h_text"))
    # существующий профиль не тронут, недостающий создан
    assert [(p["master_id"], p["name"], p["timezone"]) for p in profiles] == [(1, "Мария", "Europe/Riga"), (2, None, "Europe/Minsk")]


def test_downgrade_drops_table_and_upgrade_is_repeatable(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", "head").returncode == 0

    async def exists(conn):
        return await conn.fetchval("SELECT to_regclass('public.master_settings') IS NOT NULL")

    assert asyncio.run(_run(name, exists)) is True
    assert _alembic(url, "downgrade", BEFORE_STAGE_8).returncode == 0
    assert asyncio.run(_run(name, exists)) is False
    assert _alembic(url, "upgrade", "head").returncode == 0
    assert asyncio.run(_run(name, exists)) is True


def test_constraints_exist_in_database(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", "head").returncode == 0

    async def check(conn):
        checks = {r["conname"] for r in await conn.fetch("SELECT conname FROM pg_constraint WHERE conrelid = 'master_settings'::regclass AND contype = 'c'")}
        unique = await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE conrelid = 'master_settings'::regclass AND contype = 'u'")
        try:
            await conn.execute("UPDATE master_settings SET slot_interval_minutes = 20")
            rejected = False
        except asyncpg.CheckViolationError:
            rejected = True
        return checks, unique, rejected

    checks, unique, rejected = asyncio.run(_run(name, check))
    assert {"ck_master_settings_slot_interval", "ck_master_settings_max_active_bookings", "ck_master_settings_cancel_streak_limit", "ck_master_settings_currency_len"} <= checks
    assert unique == 1 and rejected
