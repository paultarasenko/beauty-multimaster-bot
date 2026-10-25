"""«✨ Обо мне» есть и в клиентском, и в админском меню «Информация»: клиент видит описание, мастер в админ-панели — редактор.

Роутеры подключены в порядке main.py: общий admin/content.py стоит раньше admin/about.py.
"""

import pytest
import pytest_asyncio

from app.bot.handlers.admin import about as admin_about
from app.bot.handlers.admin import content as admin_content
from app.bot.handlers.client import content as client_content
from app.bot.menu_mode import enter_admin_panel
from tests.conftest import MASTER_ID, make_client, set_owner
from tests.tg import tg_harness

OWNER_TG = 999
CLIENT_TG = 111111
ABOUT_BTN = "✨ Обо мне"
DESCRIPTION = "Мастер маникюра, 7 лет опыта"


@pytest_asyncio.fixture(autouse=True)
async def _profile(session, master_profile):
    master_profile.description = DESCRIPTION
    await session.commit()


async def test_client_sees_description(session):
    await make_client(session, CLIENT_TG)
    await session.commit()
    async with tg_harness(admin_content.router, admin_about.router, client_content.router) as h:
        await h.send_text(CLIENT_TG, ABOUT_BTN)
        assert h.tg.sent_to(CLIENT_TG)[-1] == DESCRIPTION


@pytest.mark.parametrize("in_panel, expected", [(True, "Что редактировать?"), (False, DESCRIPTION)])
async def test_owner_gets_editor_only_in_admin_panel(session, in_panel, expected):
    await set_owner(session, OWNER_TG)
    if in_panel:
        await enter_admin_panel(session, MASTER_ID, OWNER_TG)
        await session.commit()
    else:
        await make_client(session, OWNER_TG)
        await session.commit()
    async with tg_harness(admin_content.router, admin_about.router, client_content.router) as h:
        await h.send_text(OWNER_TG, ABOUT_BTN)
        assert h.tg.sent_to(OWNER_TG)[-1] == expected
