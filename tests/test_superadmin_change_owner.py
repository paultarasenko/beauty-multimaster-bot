"""Суперадмин-бот: смена владельца мастера (кнопка «Сменить владельца» в карточке) — админ-панель мастера открывается тому, чей id записан."""

import datetime as dt

import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from sqlalchemy import delete, select

from app.database.models import Master, SuperadminAction
from app.database.session import make_session_factory
from app.superadmin import service as sa
from app.superadmin.access import SuperadminAccessMiddleware
from app.superadmin.handlers import build_router as build_superadmin_router
from app.superadmin.ui import SaCallback, more_keyboard
from app.tenancy import is_owner
from tests.conftest import TEST_DATABASE_URL
from tests.stage11_support import A, B, OWNER, two_masters  # noqa: F401  (фикстура pytest)
from tests.tg import RecordingSession
from tests.webhook_utils import message_update

SUPERADMIN_ID = 424242
NEW_OWNER = 1013561296


@pytest_asyncio.fixture
async def factory(session, two_masters):
    f = make_session_factory(TEST_DATABASE_URL)
    yield f
    async with f() as s:
        await s.execute(delete(SuperadminAction))
        await s.commit()
    await f.kw["bind"].dispose()


async def _master(session, master_id: int) -> Master:
    return (
        await session.execute(select(Master).where(Master.id == master_id).execution_options(populate_existing=True))
    ).scalar_one()


async def test_set_owner_changes_only_that_master_and_returns_previous(session, two_masters, factory):
    other_before = (await _master(session, B)).owner_telegram_id

    assert await sa.set_owner_telegram_id(factory, A, NEW_OWNER) == (True, OWNER[A])

    session.expire_all()
    master_a = await _master(session, A)
    assert master_a.owner_telegram_id == NEW_OWNER
    assert is_owner(master_a, NEW_OWNER) and not is_owner(master_a, OWNER[A])  # старый владелец админ-панель больше не видит
    assert (await _master(session, B)).owner_telegram_id == other_before == OWNER[B]


async def test_set_owner_of_missing_master_changes_nothing(session, two_masters, factory):
    assert await sa.set_owner_telegram_id(factory, 999, NEW_OWNER) == (False, None)
    session.expire_all()
    assert [(await _master(session, m)).owner_telegram_id for m in (A, B)] == [OWNER[A], OWNER[B]]


async def test_more_menu_has_change_owner_button(session, two_masters, factory):
    card = await sa.get_master_card(factory, A)
    callbacks = [b.callback_data for row in more_keyboard(card).inline_keyboard for b in row]
    assert SaCallback(action="chown", master_id=A).pack() in callbacks


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "sa"},
            "chat_instance": "x",
            "data": data,
            "message": {"message_id": 1, "date": int(dt.datetime.now(dt.timezone.utc).timestamp()), "chat": {"id": user_id, "type": "private"}, "text": "card"},
        },
    }


async def test_change_owner_flow_in_superadmin_bot(session, two_masters, factory):
    tg = RecordingSession()
    bot = Bot("777000:" + "S" * 35, session=tg)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware.register(SuperadminAccessMiddleware(frozenset({SUPERADMIN_ID})))
    dp.include_router(build_superadmin_router())
    dp.workflow_data.update(session_factory=factory, registry=None, health_monitor=None, sa_bot_id=777000)

    async def feed(raw: dict) -> None:
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    await feed(_callback_update(1, SUPERADMIN_ID, SaCallback(action="chown", master_id=B).pack()))
    assert any("id нового владельца" in t for t in tg.texts())

    await feed(message_update(2, SUPERADMIN_ID, "не число"))  # неверный ввод: ничего не меняется, бот просит ещё раз
    session.expire_all()
    assert (await _master(session, B)).owner_telegram_id == OWNER[B]
    assert any("Нужно число" in t for t in tg.texts())

    await feed(message_update(3, SUPERADMIN_ID, str(NEW_OWNER)))
    session.expire_all()
    assert (await _master(session, B)).owner_telegram_id == NEW_OWNER
    assert (await _master(session, A)).owner_telegram_id == OWNER[A]
    assert any("Владелец изменён" in t for t in tg.texts())
    actions = await sa.recent_actions(factory)
    assert any(a.action == "set_owner" and a.target_master_id == B and a.actor_telegram_id == SUPERADMIN_ID for a in actions)
