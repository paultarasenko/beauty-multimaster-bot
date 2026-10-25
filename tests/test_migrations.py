"""Миграции Alembic: одна голова, схема совпадает с моделями, откат и повторный накат работают.

Идут на отдельной одноразовой БД (`<имя>_migrations_test`), а не на общей тестовой: `downgrade base` сносит таблицы,
на которых в это же время работают остальные тесты.
"""

import asyncio
import os
import subprocess
import sys
import uuid

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from tests.conftest import PROJECT_ROOT, TEST_DATABASE_URL, _check_test_database_is_safe


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
    )


def _admin_dsn(db_name: str = "postgres") -> str:
    return make_url(TEST_DATABASE_URL).set(drivername="postgresql", database=db_name).render_as_string(hide_password=False)


async def _run_admin_sql(sql: str) -> None:
    conn = await asyncpg.connect(_admin_dsn("postgres"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _public_tables(db_name: str) -> set[str]:
    conn = await asyncpg.connect(_admin_dsn(db_name))
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


@pytest.fixture
def scratch_db_url():
    _check_test_database_is_safe()
    name = f"migr_{uuid.uuid4().hex[:8]}_test"
    asyncio.run(_run_admin_sql(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False), name
    finally:
        asyncio.run(_run_admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_migrations_have_a_single_head():
    """Две головы = две ветки миграций: `alembic upgrade head` на проде падает с «Multiple head revisions»."""
    heads = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"несколько голов миграций: {heads}"


def test_models_match_migrations(scratch_db_url):
    """Забытая миграция: модель поменяли, а миграцию не написали — на проде схема разойдётся с кодом."""
    url, _ = scratch_db_url
    assert _alembic(url, "upgrade", "head").returncode == 0
    result = _alembic(url, "check")
    assert result.returncode == 0, f"alembic check:\n{result.stdout}\n{result.stderr}"


def test_full_downgrade_and_upgrade_roundtrip(scratch_db_url):
    """Каждая миграция откатывается; после полного отката остаётся только служебная таблица, накат повторяется."""
    url, name = scratch_db_url
    for step in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
        result = _alembic(url, *step)
        assert result.returncode == 0, f"alembic {' '.join(step)}:\n{result.stdout}\n{result.stderr}"
        if step == ["downgrade", "base"]:
            assert asyncio.run(_public_tables(name)) <= {"alembic_version"}
    assert {"users", "bookings", "services", "notifications"} <= asyncio.run(_public_tables(name))
