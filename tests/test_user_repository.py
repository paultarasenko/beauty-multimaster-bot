import pytest
from sqlalchemy import text

from app.database.models import User
from app.database.repositories.user_repository import UserRepository
from tests.conftest import MASTER_ID, make_client


async def _add_users(session, *usernames: str | None) -> None:
    for i, username in enumerate(usernames, start=1):
        await make_client(session, 1000 + i, username=username)
    await session.commit()


async def test_username_lookup_is_case_insensitive(session):
    await _add_users(session, "Anna_K")
    found = await UserRepository(session, MASTER_ID).get_by_username("anna_k")
    assert found is not None and found.username == "Anna_K"


@pytest.mark.parametrize("pattern", ["anna_k", "anna%", "%", "_nna_k", "anna_"])
async def test_username_lookup_treats_wildcards_literally(session, pattern):
    """`_` и `%` — обычные символы: «anna_k» не должен находить «annaxk», а «%» — вообще никого."""
    await _add_users(session, "annaxk")
    assert await UserRepository(session, MASTER_ID).get_by_username(pattern) is None


async def test_username_with_underscore_does_not_clash_with_lookalike(session):
    """Раньше это падало с MultipleResultsFound и ломало ручную запись мастера по @username."""
    await _add_users(session, "anna_k", "annaxk")
    found = await UserRepository(session, MASTER_ID).get_by_username("anna_k")
    assert found is not None and found.username == "anna_k"


async def test_stale_duplicate_username_returns_most_recently_updated_not_error(session):
    """Ник сменили, а освободившийся занял другой человек: у старой строки он остаётся — это не должно ронять поиск."""
    await _add_users(session, "olga", "olga")
    await session.execute(
        text("UPDATE users SET updated_at = now() - interval '30 days' WHERE telegram_id = 1001")
    )
    await session.commit()
    found = await UserRepository(session, MASTER_ID).get_by_username("olga")
    assert found is not None and found.telegram_id == 1002


async def test_unknown_username_returns_none(session):
    await _add_users(session, "anna_k", None)
    assert await UserRepository(session, MASTER_ID).get_by_username("nobody") is None
