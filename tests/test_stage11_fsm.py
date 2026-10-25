"""Этап 11, FSM: один человек одновременно в боте A и в боте B — состояния и данные раздельные (memory и настоящий Redis).

Ключи Redis: bbot:fsm:<bot_id>:<chat_id>:<user_id>:state|data. Очистка при remove-bot затрагивает только состояния этого бота.
"""

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import select

from app.bot.fsm_storage import clear_bot_states, create_fsm_backend
from app.database.models import MasterBot
from app.database.session import make_session_factory
from app.multibot import management
from tests import fsm_utils
from tests.conftest import TEST_DATABASE_URL
from tests.stage11_support import A, B, BOT_ID, snapshot, two_masters  # noqa: F401  (фикстура pytest)
from tests.webhook_utils import FakeTelegram

PERSON = 70001  # один и тот же человек (chat_id == user_id в личке) в обоих ботах


class Flow(StatesGroup):
    step_a = State()
    step_b = State()


@pytest_asyncio.fixture(params=["memory", "redis"])
async def backend(request):
    if request.param == "redis":
        request.getfixturevalue("redis_url")  # пропуск ТОЛЬКО если Redis недоступен
        prefix = request.getfixturevalue("fsm_prefix")
        made = create_fsm_backend(kind="redis", redis_url=fsm_utils.TEST_REDIS_URL, state_ttl=600, prefix=prefix)
    else:
        prefix = None
        made = create_fsm_backend(kind="memory")
    made.test_prefix = prefix
    yield made
    await made.storage.close()


def ctx(backend, bot_id: int, user_id: int = PERSON) -> FSMContext:
    return FSMContext(backend.storage, StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id))


async def test_same_person_has_independent_state_and_data_in_two_bots(backend):
    a, b = ctx(backend, BOT_ID[A]), ctx(backend, BOT_ID[B])
    await a.set_state(Flow.step_a)
    await a.update_data(service_id=11, note="данные бота A")
    assert await b.get_state() is None and await b.get_data() == {}  # у B ничего нет

    await b.set_state(Flow.step_b)
    await b.update_data(service_id=22)
    assert await a.get_state() == Flow.step_a.state and await b.get_state() == Flow.step_b.state
    assert await a.get_data() == {"service_id": 11, "note": "данные бота A"}
    assert await b.get_data() == {"service_id": 22}

    await a.clear()  # сброс у A не трогает B
    assert await a.get_state() is None and await a.get_data() == {}
    assert await b.get_state() == Flow.step_b.state and await b.get_data() == {"service_id": 22}


async def test_different_people_in_one_bot_do_not_share_state(backend):
    await ctx(backend, BOT_ID[A], 1).set_state(Flow.step_a)
    assert await ctx(backend, BOT_ID[A], 2).get_state() is None


async def test_redis_keys_always_carry_bot_id(backend):
    for master in (A, B):
        await ctx(backend, BOT_ID[master]).set_state(Flow.step_a)
        await ctx(backend, BOT_ID[master]).update_data(x=1)
    if backend.test_prefix is None:  # memory: ключ хранилища — StorageKey, bot_id в нём обязателен
        assert {key.bot_id for key in backend.storage.storage} == {BOT_ID[A], BOT_ID[B]}
        return
    keys = fsm_utils.sync_keys(backend.test_prefix)
    assert keys, "в Redis нет ключей FSM"
    for master in (A, B):
        mine = [k for k in keys if k.startswith(f"{backend.test_prefix}:{BOT_ID[master]}:")]
        assert {k.rsplit(":", 1)[1] for k in mine} >= {"state", "data"}
    assert all(any(k.startswith(f"{backend.test_prefix}:{BOT_ID[m]}:") for m in (A, B)) for k in keys), "ключ без bot_id"


async def test_clear_bot_states_touches_only_that_bot_and_not_prefix_neighbours(backend):
    """В том числе «похожий» bot_id: очистка бота 810 не должна задеть бота 8101 (префикс числа)."""
    short, long_ = 810, 8101
    for bot_id in (short, long_, BOT_ID[B]):
        for user in (1, 2, 3):
            await ctx(backend, bot_id, user).set_state(Flow.step_a)
            await ctx(backend, bot_id, user).update_data(v=bot_id)
    assert await clear_bot_states(backend.storage, short) >= 3
    for user in (1, 2, 3):
        assert await ctx(backend, short, user).get_state() is None
        assert await ctx(backend, short, user).get_data() == {}
        assert await ctx(backend, long_, user).get_data() == {"v": long_}
        assert await ctx(backend, BOT_ID[B], user).get_data() == {"v": BOT_ID[B]}


async def test_remove_bot_clears_fsm_of_that_bot_only(session, two_masters, backend):
    ns = two_masters
    for bot_id in (BOT_ID[A], BOT_ID[B]):
        for user in (PERSON, 5, 6):
            await ctx(backend, bot_id, user).set_state(Flow.step_a)
            await ctx(backend, bot_id, user).update_data(owner_bot=bot_id)

    async def cleaner(bot_id: int) -> int:
        return await clear_bot_states(backend.storage, bot_id)

    factory = make_session_factory(TEST_DATABASE_URL)
    tg = FakeTelegram()
    before_a = await snapshot(session, A)
    try:
        warning = await management.remove_bot(
            factory, ns.cipher, master_id=B, bot_factory=lambda token: Bot(token, session=tg), fsm_cleaner=cleaner
        )
    finally:
        await factory.kw["bind"].dispose()
    assert warning is None

    for user in (PERSON, 5, 6):
        assert await ctx(backend, BOT_ID[B], user).get_state() is None  # диалоги удалённого бота стёрты
        assert await ctx(backend, BOT_ID[A], user).get_data() == {"owner_bot": BOT_ID[A]}  # у A — как были
    session.expire_all()
    assert (await session.execute(select(MasterBot.master_id))).scalars().all() == [A]  # строка бота B удалена, A цела
    assert await snapshot(session, A) == before_a
