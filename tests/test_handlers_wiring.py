"""Сборка бота: админские роутеры закрыты фильтром, middleware ведут себя как описано."""

import importlib
import pkgutil

import pytest
import pytest_asyncio
from aiogram import F, Router
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message, Update
from sqlalchemy import select

import app.bot.handlers.admin as admin_package
from app.bot.handlers.admin import services as admin_services
from app.bot.handlers.client import content as client_content
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.middlewares.error_handling import GENERIC_ERROR_TEXT
from app.bot.middlewares.reset_input import menu_button_texts
from app.bot.states.booking_states import AdminServiceFSM
from app.database.models import Service, User
from tests.conftest import set_owner
from tests.tg import tg_harness

ADMIN_ID = 999
STRANGER_ID = 555

# Единственный админский роутер без фильтра: он обслуживает кнопки меню, которые есть и у клиента, и у мастера
# (прайс, портфолио, «О мастере»…), а мастерские добавки показывает по флагу is_in_admin_panel.
# Любой новый файл в handlers/admin по умолчанию обязан быть закрыт IsAdminFilter.
ROUTERS_WITHOUT_ADMIN_FILTER = {"content": {"message"}}

EVENTS = ("message", "callback_query", "edited_message", "inline_query", "chat_member", "my_chat_member")


def _admin_routers() -> list[tuple[str, Router]]:
    found = []
    for module_info in pkgutil.iter_modules(admin_package.__path__):
        module = importlib.import_module(f"{admin_package.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        assert isinstance(router, Router), f"{module_info.name}: в модуле нет router"
        found.append((module_info.name, router))
    return found


def test_every_admin_router_is_found():
    assert len(_admin_routers()) >= 11


@pytest.mark.parametrize(("name", "router"), _admin_routers(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_admin_handler_is_behind_the_admin_filter(name, router):
    """Без IsAdminFilter хендлер доступен любому, кто пришлёт нужный текст/callback_data (их легко подделать)."""
    unguarded = set()
    for event_name in EVENTS:
        observer = router.observers[event_name]
        if not observer.handlers:
            continue
        if not any(isinstance(f.callback, IsAdminFilter) for f in observer._handler.filters):
            unguarded.add(event_name)
    assert unguarded == ROUTERS_WITHOUT_ADMIN_FILTER.get(name, set()), (
        f"admin/{name}.py: обработчики {sorted(unguarded)} не закрыты IsAdminFilter. "
        "Добавьте router.message.filter(IsAdminFilter()) / router.callback_query.filter(IsAdminFilter())."
    )


async def test_stranger_cannot_reach_admin_handlers(session):
    """Сквозная проверка: тот, кто не админ, не может ни добавить услугу, ни продвинуть админский FSM."""
    await set_owner(session, ADMIN_ID)
    async with tg_harness(admin_services.router) as h:
        await h.set_state(STRANGER_ID, AdminServiceFSM.enter_name)
        await h.send_text(STRANGER_ID, "Хакерская услуга")
        await h.press(STRANGER_ID, "svc_add")
        assert await h.get_state(STRANGER_ID) == AdminServiceFSM.enter_name.state
        assert h.tg.requests == []
    assert (await session.execute(select(Service))).scalars().all() == []


# --- ResetInputOnMenuPressMiddleware ---


def test_menu_button_texts_are_collected_from_keyboards():
    texts = menu_button_texts()
    assert {"💰 Услуги и цены", "📅 Записаться", "ℹ️ Информация"} <= texts
    assert all(texts) and len(texts) > 10


@pytest_asyncio.fixture
async def admin(session):
    await set_owner(session, ADMIN_ID)


@pytest.mark.parametrize("pressed", ["💰 Услуги и цены", "/start", "/anything", "  📅 Записаться  "])
async def test_admin_input_is_reset_when_menu_button_or_command_is_pressed(session, admin, pressed):
    async with tg_harness(admin_services.router, client_content.router, reset_input=True) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_name)
        await h.send_text(ADMIN_ID, pressed)

        assert await h.get_state(ADMIN_ID) is None, "кнопка меню должна прервать ввод"
    assert (await session.execute(select(Service))).scalars().all() == [], "текст кнопки не должен стать названием услуги"


async def test_admin_regular_text_still_goes_to_the_input_handler(session, admin):
    async with tg_harness(admin_services.router, reset_input=True) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_name)
        await h.send_text(ADMIN_ID, "Педикюр")

        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_price.state


async def test_reset_does_nothing_for_non_admin_or_without_state(session, admin):
    async with tg_harness(client_content.router, reset_input=True) as h:
        sample = State(state="Other:state")
        await h.set_state(STRANGER_ID, sample)
        await h.send_text(STRANGER_ID, "💰 Услуги и цены")
        assert await h.get_state(STRANGER_ID) == sample.state, "чужое состояние не трогаем"

        await h.send_text(ADMIN_ID, "💰 Услуги и цены")  # админ без состояния — просто отрабатывает кнопка
        assert await h.get_state(ADMIN_ID) is None


# --- ErrorHandlingMiddleware ---


def _crashing_router() -> Router:
    router = Router(name="crash")

    @router.message(F.text == "boom")
    async def boom_message(message: Message):
        raise RuntimeError("boom")

    @router.message(F.text == "ok")
    async def ok(message: Message):
        await message.answer("fine")

    @router.callback_query(F.data == "boom")
    async def boom_callback(callback: CallbackQuery):
        raise RuntimeError("boom")

    @router.edited_message()
    async def boom_edited(message: Message):
        raise RuntimeError("boom")

    return router


async def test_crash_in_message_handler_gives_user_a_generic_message(session, caplog):
    async with tg_harness(_crashing_router(), error_middleware=True) as h:
        await h.send_text(STRANGER_ID, "boom")

        assert h.tg.sent_to(STRANGER_ID) == [GENERIC_ERROR_TEXT]
    assert "Unhandled error while processing update" in caplog.text


async def test_crash_in_callback_handler_answers_the_button_with_an_alert(session):
    async with tg_harness(_crashing_router(), error_middleware=True) as h:
        await h.press(STRANGER_ID, "boom")

        assert h.tg.alerts() == [GENERIC_ERROR_TEXT]


async def test_crash_in_update_without_message_or_callback_is_swallowed_quietly(session):
    async with tg_harness(_crashing_router(), error_middleware=True) as h:
        message = h._message(STRANGER_ID, "boom")
        await h.dp.feed_update(h.bot, Update(update_id=1, edited_message=message))
        assert h.tg.requests == []


async def test_failure_to_notify_user_about_error_does_not_propagate(session):
    async with tg_harness(_crashing_router(), error_middleware=True) as h:
        h.tg.blocked_chat_ids.add(STRANGER_ID)
        await h.send_text(STRANGER_ID, "boom")  # не бросает


async def test_healthy_update_passes_through_untouched(session):
    async with tg_harness(_crashing_router(), error_middleware=True) as h:
        await h.send_text(STRANGER_ID, "ok")
        assert h.tg.sent_to(STRANGER_ID) == ["fine"]


# --- DatabaseSessionMiddleware ---


def _db_router() -> Router:
    router = Router(name="db")

    @router.message(F.text == "save")
    async def save(message: Message, session):
        session.add(User(telegram_id=7001, first_name="saved"))

    @router.message(F.text == "crash")
    async def crash(message: Message, session):
        session.add(User(telegram_id=7002, first_name="lost"))
        await session.flush()
        raise RuntimeError("after flush")

    return router


async def test_session_is_committed_after_successful_handler(session):
    async with tg_harness(_db_router()) as h:
        await h.send_text(STRANGER_ID, "save")
    session.expire_all()
    assert [u.telegram_id for u in (await session.execute(select(User))).scalars()] == [7001]


async def test_session_is_not_committed_when_handler_crashes(session):
    async with tg_harness(_db_router()) as h:
        with pytest.raises(RuntimeError):
            await h.send_text(STRANGER_ID, "crash")
    session.expire_all()
    assert (await session.execute(select(User))).scalars().all() == [], "частично выполненный хендлер не должен сохраняться"
