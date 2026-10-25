"""Этап 3 multi-tenant: владелец мастера = masters.owner_telegram_id.

Права, получатель уведомлений и режим меню определяются мастером апдейта; ADMIN_TELEGRAM_ID остаётся только
начальной загрузкой владельца при старте.
"""

import datetime as dt
import logging

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Chat, ChatMemberBanned, ChatMemberMember, ChatMemberUpdated, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot.handlers.admin import services as admin_services
from app.bot.handlers.client import bot_blocked
from app.bot.menu_mode import enter_admin_panel, is_in_admin_panel, leave_admin_panel
from app.bot.middlewares.admin_filter import IsAdminFilter, get_admin_contact_username
from app.bot.states.booking_states import AdminServiceFSM
from app.config.settings import settings
from app.database.models import Master, MasterClient, Service, User
from app.database.repositories.user_repository import UserRepository
from app.database.session import make_session_factory
from app.services.notifier import MasterNotifier
from app.tenancy import TenantContext, bootstrap_owner, get_owner_telegram_id, is_owner
from tests.conftest import MASTER_ID, TEST_DATABASE_URL, make_client, set_owner
from tests.tg import BOT_ID, RecordingSession, tg_harness

A, B = 1, 2
OWNER_A = 999
OWNER_B = 777
CLIENT = 555


def _tg_user(user_id: int) -> TgUser:
    return TgUser(id=user_id, is_bot=False, first_name=f"user{user_id}")


def _message(user_id: int) -> Message:
    return Message(
        message_id=1, date=dt.datetime.now(dt.timezone.utc), chat=Chat(id=user_id, type="private"),
        from_user=_tg_user(user_id), text="x",
    )


def _callback(user_id: int) -> CallbackQuery:
    return CallbackQuery(id="1", from_user=_tg_user(user_id), chat_instance="ci", data="x")


def _tenant(master_id: int, owner: int | None) -> TenantContext:
    return TenantContext(master_id=master_id, master=Master(id=master_id, owner_telegram_id=owner))


@pytest_asyncio.fixture
async def two_masters(session):
    """Мастер №1 (владелец OWNER_A) и мастер №2 (владелец OWNER_B)."""
    session.add(Master(id=B, owner_telegram_id=OWNER_B))
    await session.flush()
    await set_owner(session, OWNER_A, A)


def _bot() -> tuple[Bot, RecordingSession]:
    tg = RecordingSession()
    return Bot(token=f"{BOT_ID}:TEST", session=tg), tg


def _blocked_update(user_id: int) -> Update:
    bot_user = TgUser(id=BOT_ID, is_bot=True, first_name="bot")
    return Update(
        update_id=1,
        my_chat_member=ChatMemberUpdated(
            chat=Chat(id=user_id, type="private"),
            from_user=_tg_user(user_id),
            date=dt.datetime.now(dt.timezone.utc),
            old_chat_member=ChatMemberMember(status=ChatMemberStatus.MEMBER, user=bot_user),
            new_chat_member=ChatMemberBanned(status=ChatMemberStatus.KICKED, user=bot_user, until_date=0),
        ),
    )


# --- is_owner / IsAdminFilter ---


def test_is_owner_compares_with_owner_of_that_master_only():
    master_a, master_b = Master(id=A, owner_telegram_id=OWNER_A), Master(id=B, owner_telegram_id=OWNER_B)
    assert is_owner(master_a, OWNER_A) and not is_owner(master_b, OWNER_A)
    assert is_owner(master_b, OWNER_B) and not is_owner(master_a, OWNER_B)


@pytest.mark.parametrize("telegram_id", [None, 0, OWNER_A])
def test_master_without_owner_has_no_owner(telegram_id):
    assert not is_owner(Master(id=A, owner_telegram_id=None), telegram_id)


async def test_owner_passes_filter_for_message_and_callback():
    f, tenant = IsAdminFilter(), _tenant(A, OWNER_A)
    assert await f(_message(OWNER_A), tenant=tenant) is True
    assert await f(_callback(OWNER_A), tenant=tenant) is True


async def test_plain_client_does_not_pass_filter_for_message_nor_callback():
    f, tenant = IsAdminFilter(), _tenant(A, OWNER_A)
    assert await f(_message(CLIENT), tenant=tenant) is False
    assert await f(_callback(CLIENT), tenant=tenant) is False


async def test_owner_of_a_is_not_admin_for_master_b():
    f = IsAdminFilter()
    assert await f(_message(OWNER_A), tenant=_tenant(B, OWNER_B)) is False
    assert await f(_callback(OWNER_A), tenant=_tenant(B, OWNER_B)) is False
    assert await f(_message(OWNER_B), tenant=_tenant(B, OWNER_B)) is True


async def test_filter_denies_without_tenant_and_for_master_without_owner():
    f = IsAdminFilter()
    assert await f(_message(OWNER_A)) is False  # нет TenantMiddleware — доступ закрыт, а не открыт
    assert await f(_message(OWNER_A), tenant=_tenant(A, None)) is False
    assert await f(_message(0), tenant=_tenant(A, None)) is False


async def test_owner_is_admin_in_bot_a_and_not_in_bot_b_end_to_end(session, two_masters):
    """Сквозной прогон через настоящий диспетчер: один и тот же человек, два бота (мастера)."""
    async with tg_harness(admin_services.router, master_id=A) as h:
        await h.press(OWNER_A, "svc_add")
        assert await h.get_state(OWNER_A) == AdminServiceFSM.enter_name.state
        assert h.tg.requests != []

    async with tg_harness(admin_services.router, master_id=B) as h:
        await h.press(OWNER_A, "svc_add")
        await h.set_state(OWNER_A, AdminServiceFSM.enter_name)
        await h.send_text(OWNER_A, "Хакерская услуга")
        assert h.tg.requests == []
        assert await h.get_state(OWNER_A) == AdminServiceFSM.enter_name.state  # состояние не сдвинулось

        await h.press(OWNER_B, "svc_add")  # а владелец B в своём боте — админ
        assert h.tg.requests != []
    assert (await session.execute(select(Service))).scalars().all() == []


async def test_removing_owner_revokes_admin_rights(session):
    await set_owner(session, OWNER_A)
    async with tg_harness(admin_services.router) as h:
        await h.press(OWNER_A, "svc_add")
        assert h.tg.requests != []
    await set_owner(session, None)
    async with tg_harness(admin_services.router) as h:
        await h.press(OWNER_A, "svc_add")
        assert h.tg.requests == []


# --- контакт мастера ---


async def test_contact_username_is_taken_from_owner_of_that_master():
    bot, tg = _bot()
    assert await get_admin_contact_username(bot, Master(id=B, owner_telegram_id=OWNER_B)) == "master_test"
    [request] = [r for r in tg.requests if type(r).__name__ == "GetChat"]
    assert request.chat_id == OWNER_B


async def test_contact_username_without_owner_is_none_and_does_not_call_telegram():
    bot, tg = _bot()
    assert await get_admin_contact_username(bot, Master(id=A, owner_telegram_id=None)) is None
    assert tg.requests == []


# --- MasterNotifier ---


async def test_notifications_go_to_owner_of_own_master_only():
    bot, tg = _bot()
    notifier_a = MasterNotifier(bot, master_id=A, owner_telegram_id=OWNER_A)
    notifier_b = MasterNotifier(bot, master_id=B, owner_telegram_id=OWNER_B)
    client = User(id=1, telegram_id=CLIENT, first_name="Анна")

    await notifier_a.notify_new_user(client)
    await notifier_b.notify_client_blocked_bot(client)

    assert len(tg.sent_to(OWNER_A)) == 1 and "Новый пользователь" in tg.sent_to(OWNER_A)[0]
    assert len(tg.sent_to(OWNER_B)) == 1 and "заблокировал бота" in tg.sent_to(OWNER_B)[0]


async def test_notifier_requests_go_to_owner_and_report_delivery():
    from types import SimpleNamespace

    bot, tg = _bot()
    request = SimpleNamespace(id=5, message="Можно ещё запись?")
    client = User(id=1, telegram_id=CLIENT, first_name="Анна")

    assert await MasterNotifier(bot, master_id=B, owner_telegram_id=OWNER_B).notify_limit_request(request, client) is True
    assert await MasterNotifier(bot, master_id=B, owner_telegram_id=OWNER_B).notify_access_request(request, client) is True
    assert len(tg.sent_to(OWNER_B)) == 2 and tg.sent_to(OWNER_A) == []

    # мастер без владельца: доставить некому — как раньше при ADMIN_TELEGRAM_ID=0
    nobody = MasterNotifier(bot, master_id=A, owner_telegram_id=None)
    assert await nobody.notify_limit_request(request, client) is False
    assert await nobody.notify_access_request(request, client) is False
    assert len(tg.requests) == 2


async def test_master_without_owner_does_not_crash_and_logs(caplog):
    bot, tg = _bot()
    notifier = MasterNotifier(bot, master_id=A, owner_telegram_id=None)
    with caplog.at_level(logging.WARNING):
        await notifier.notify_new_user(User(id=1, telegram_id=CLIENT, first_name="Анна"))
    assert tg.requests == []
    assert "owner_telegram_id" in caplog.text


async def test_handler_notifies_owner_of_the_update_master(session, two_masters):
    """Клиент заблокировал бота мастера B — уведомление получает владелец B, а не владелец A."""
    async with tg_harness(bot_blocked.router, master_id=B) as h:
        await h.dp.feed_update(h.bot, _blocked_update(CLIENT))
        assert len(h.tg.sent_to(OWNER_B)) == 1
        assert h.tg.sent_to(OWNER_A) == []


async def test_handler_for_master_without_owner_sends_nothing_and_does_not_fail(session):
    async with tg_harness(bot_blocked.router, master_id=A) as h:  # у мастера №1 в этом тесте владельца нет
        await h.dp.feed_update(h.bot, _blocked_update(CLIENT))
        assert h.tg.requests == []


async def test_owner_blocking_own_bot_is_not_reported_but_foreign_owner_is_just_a_client(session, two_masters):
    async with tg_harness(bot_blocked.router, master_id=A) as h:
        await h.dp.feed_update(h.bot, _blocked_update(OWNER_A))  # владелец сам заблокировал бота
        assert h.tg.requests == []
    async with tg_harness(bot_blocked.router, master_id=B) as h:
        await h.dp.feed_update(h.bot, _blocked_update(OWNER_A))  # для мастера B владелец A — обычный клиент
        assert len(h.tg.sent_to(OWNER_B)) == 1


# --- bootstrap из ADMIN_TELEGRAM_ID ---


@pytest.fixture
def factory():
    return make_session_factory(TEST_DATABASE_URL)


async def _owner(session, master_id: int = MASTER_ID) -> int | None:
    session.expire_all()
    return await get_owner_telegram_id(session, master_id)


async def test_bootstrap_writes_env_admin_when_owner_is_empty(session, factory, monkeypatch, caplog):
    monkeypatch.setattr(settings, "admin_telegram_id", OWNER_A)
    monkeypatch.setattr(settings, "master_id", A)
    with caplog.at_level(logging.INFO):
        assert await bootstrap_owner(factory) == OWNER_A
    assert await _owner(session) == OWNER_A
    assert "ADMIN_TELEGRAM_ID" in caplog.text


async def test_bootstrap_does_not_overwrite_existing_owner(session, factory, monkeypatch):
    await set_owner(session, OWNER_B)
    monkeypatch.setattr(settings, "admin_telegram_id", OWNER_A)
    monkeypatch.setattr(settings, "master_id", A)
    assert await bootstrap_owner(factory) == OWNER_B
    assert await _owner(session) == OWNER_B


async def test_bootstrap_without_owner_anywhere_warns_and_does_not_fail(session, factory, monkeypatch, caplog):
    monkeypatch.setattr(settings, "admin_telegram_id", 0)
    monkeypatch.setattr(settings, "master_id", A)
    with caplog.at_level(logging.WARNING):
        assert await bootstrap_owner(factory) is None
    assert await _owner(session) is None
    assert "не задан владелец" in caplog.text


async def test_bootstrap_touches_only_the_configured_master(session, factory, monkeypatch):
    session.add(Master(id=B))
    await session.commit()
    monkeypatch.setattr(settings, "admin_telegram_id", OWNER_A)
    monkeypatch.setattr(settings, "master_id", B)
    await bootstrap_owner(factory)
    assert await _owner(session, B) == OWNER_A
    assert await _owner(session, A) is None


async def test_bootstrap_with_missing_master_is_a_noop(session, factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_telegram_id", OWNER_A)
    monkeypatch.setattr(settings, "master_id", 424242)
    assert await bootstrap_owner(factory) is None  # отсутствие мастера обработает ensure_tenant_available


async def test_env_admin_id_alone_gives_no_rights(session):
    """ADMIN_TELEGRAM_ID больше не источник прав: без записи в masters.owner_telegram_id админа нет."""
    settings_before = settings.admin_telegram_id
    settings.admin_telegram_id = OWNER_A
    try:
        async with tg_harness(admin_services.router) as h:
            await h.press(OWNER_A, "svc_add")
            assert h.tg.requests == []
    finally:
        settings.admin_telegram_id = settings_before


# --- режим меню (is_in_admin_panel) по мастеру ---


async def test_admin_panel_flag_is_independent_for_owner_of_a_and_client_of_b(session, two_masters):
    await make_client(session, OWNER_A, B)  # владелец A — ещё и клиент мастера B
    await session.commit()

    await enter_admin_panel(session, A, OWNER_A)
    await session.commit()
    assert await is_in_admin_panel(session, A, OWNER_A) is True
    assert await is_in_admin_panel(session, B, OWNER_A) is False

    await enter_admin_panel(session, B, OWNER_A)
    await leave_admin_panel(session, A, OWNER_A)
    await session.commit()
    assert await is_in_admin_panel(session, A, OWNER_A) is False
    assert await is_in_admin_panel(session, B, OWNER_A) is True


async def test_admin_panel_flag_is_stored_in_master_clients_and_person_is_linked(session, two_masters):
    await enter_admin_panel(session, A, OWNER_A)  # человека ещё нет в базе — создаётся вместе со связью
    await session.commit()

    user = (await session.execute(select(User).where(User.telegram_id == OWNER_A))).scalar_one()
    link = (await session.execute(select(MasterClient).where(MasterClient.user_id == user.id))).scalar_one()
    assert (link.master_id, link.is_in_admin_panel) == (A, True)
    assert not hasattr(User, "is_in_admin_panel")
    assert await UserRepository(session, B).get_by_telegram_id(OWNER_A) is None  # мастеру B человек по-прежнему чужой
    assert await is_in_admin_panel(session, B, OWNER_A) is False


async def test_admin_panel_flag_for_unknown_person_is_false(session):
    assert await is_in_admin_panel(session, A, 31337) is False


# --- неактивный мастер ---


async def test_inactive_master_still_drops_updates_even_from_its_owner(session):
    await set_owner(session, OWNER_A)
    master = await session.get(Master, A)
    master.is_active = False
    await session.commit()
    async with tg_harness(admin_services.router) as h:
        await h.press(OWNER_A, "svc_add")
        await h.send_text(OWNER_A, "текст")
        assert h.tg.requests == []
        assert await h.get_state(OWNER_A) is None
