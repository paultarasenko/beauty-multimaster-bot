"""Этап 7: сценарий «Обо мне» (AdminAboutFSM) через настоящие хендлеры на memory и на Redis (в других тестах его нет)."""

import pytest
from sqlalchemy import select

from app.bot.handlers.admin import about as admin_about
from app.bot.keyboards.callback_data import AdminAboutCallback
from app.bot.states.booking_states import AdminAboutFSM
from app.database.models import MasterProfile
from tests.conftest import MASTER_ID, set_owner
from tests.tg import tg_harness

OWNER = 900


pytestmark = pytest.mark.parametrize("fsm_kind", ["memory", "redis"], indirect=True)


async def test_edit_address_flow_keeps_field_in_fsm_and_saves(session, master_profile):
    await set_owner(session, OWNER)
    async with tg_harness(admin_about.router) as h:
        await h.press(OWNER, AdminAboutCallback(action="edit", field="address"))
        assert await h.get_state(OWNER) == AdminAboutFSM.edit_field.state
        assert await h.fsm(OWNER).get_data() == {"field": "address"}

        await h.send_text(OWNER, "ул. Тестовая, 5")
        assert await h.get_state(OWNER) is None and await h.fsm(OWNER).get_data() == {}
    await session.rollback()
    profile = (await session.execute(select(MasterProfile).where(MasterProfile.master_id == MASTER_ID))).scalar_one()
    assert profile.address == "ул. Тестовая, 5"


async def test_cancel_clears_state(session, master_profile):
    await set_owner(session, OWNER)
    async with tg_harness(admin_about.router) as h:
        await h.press(OWNER, AdminAboutCallback(action="edit", field="description"))
        await h.press(OWNER, AdminAboutCallback(action="cancel", field="none"))
        assert await h.get_state(OWNER) is None
