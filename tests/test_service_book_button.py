"""«Услуги и цены» -> «📅 Записаться» под услугой: запись сразу на эту услугу (настоящий диспетчер и БД)."""

import datetime as dt

import pytest
import pytest_asyncio
from aiogram.methods import DeleteMessage, DeleteMessages, EditMessageText, SendMessage

from app.bot.handlers.admin import content as admin_content
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import content as client_content
from app.bot.keyboards.callback_data import DateCallback, ServiceBookCallback
from app.bot.screen_tracking import ScreenStore, screen_tracking
from app.bot.screens import ScreenCleanupMiddleware
from app.bot.states.booking_states import BookingFSM
from app.database.models import Service, WorkingHours
from tests.conftest import MASTER_ID, next_weekday, set_client
from tests.tg import tg_harness

CLIENT_TG = 111111  # test_user
PRICE_BTN = "💰 Услуги и цены"


@pytest_asyncio.fixture
async def world(session, master_profile, test_user):
    """Две активные услуги и рабочий день (пятница) через 2+ недели."""
    day = next_weekday(4)
    session.add(WorkingHours(master_id=MASTER_ID, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    manicure = Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0)
    brows = Service(master_id=MASTER_ID, name="Брови", price=25, duration_minutes=30, active=True, sort_order=1)
    session.add_all([manicure, brows])
    await session.commit()
    return {"day": day, "manicure_id": manicure.id, "brows_id": brows.id}


def _sent(h):
    return [m for m in h.tg.requests if isinstance(m, SendMessage)]


def _with_screen_cleanup(h) -> None:
    """Как в боте: учёт отправленных сообщений и очистка прежних экранов (app/bot/screens.py)."""
    h.tg.middleware(screen_tracking)
    h.dp.update.middleware(ScreenCleanupMiddleware(ScreenStore.for_storage(h.storage)))


def _message_id(h, request) -> int:
    """id сообщения, которое стенд вернул на этот SendMessage (tests/tg.py: id = порядковый номер запроса)."""
    return h.tg.requests.index(request) + 1


def _deleted_ids(h, since: int) -> set[int]:
    ids: set[int] = set()
    for m in h.tg.requests[since:]:
        if isinstance(m, DeleteMessages):
            ids.update(m.message_ids)
        elif isinstance(m, DeleteMessage):
            ids.add(m.message_id)
    return ids


@pytest.mark.parametrize("price_router", [admin_content.router, client_content.router], ids=["admin_content", "client_content"])
async def test_every_service_message_has_its_own_book_button(session, world, price_router):
    async with tg_harness(price_router) as h:
        await h.send_text(CLIENT_TG, PRICE_BTN)
    sent = _sent(h)
    assert len(sent) == 2
    for message, service_id in zip(sent, (world["manicure_id"], world["brows_id"])):
        [[button]] = message.reply_markup.inline_keyboard
        assert button.text == "📅 Записаться"
        assert button.callback_data == ServiceBookCallback(service_id=service_id).pack()


async def test_button_opens_calendar_for_that_service_in_a_new_message(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=world["brows_id"]))

        assert await h.get_state(CLIENT_TG) == BookingFSM.select_date.state
        data = await h.fsm(CLIENT_TG).get_data()
        assert data["service_id"] == world["brows_id"] and data["preselected_date"] is None
    [text] = h.tg.sent_to(CLIENT_TG)
    assert text.startswith("Брови\n\n📅 Выберите дату:")
    assert not [m for m in h.tg.requests if isinstance(m, EditMessageText)]  # календарь — новое сообщение, а не правка карточки


async def test_flow_continues_to_time_selection_for_the_chosen_service(session, world):
    day = world["day"]
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=world["manicure_id"]))
        await h.press(CLIENT_TG, DateCallback(date=day.isoformat()))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_time.state
        shown = h.tg.edited()[-1]
        assert "Маникюр" in shown and f"📅 {day.strftime('%d.%m.%Y')}" in shown and "🕐 Свободное время:" in shown


async def test_button_works_from_any_previous_state(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_service, preselected_date="2030-01-01")
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=world["manicure_id"]))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_date.state
        data = await h.fsm(CLIENT_TG).get_data()
        assert data["service_id"] == world["manicure_id"] and data["preselected_date"] is None


async def test_inactive_service_is_refused(session, world):
    session.expire_all()
    (await session.get(Service, world["brows_id"])).active = False
    await session.commit()
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=world["brows_id"]))
        assert h.tg.alerts() == ["Эта услуга больше недоступна."]
        assert await h.get_state(CLIENT_TG) is None
        assert h.tg.sent_to(CLIENT_TG) == []


async def test_unknown_service_is_refused(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=987654))
        assert h.tg.alerts() == ["Эта услуга больше недоступна."]
        assert await h.get_state(CLIENT_TG) is None


async def test_restricted_client_gets_the_usual_refusal(session, world, test_user):
    await set_client(session, test_user, is_restricted=True)
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=world["manicure_id"]))
        assert h.tg.sent_to(CLIENT_TG) == [client_booking.RESTRICTED_TEXT]
        assert await h.get_state(CLIENT_TG) is None


async def test_no_free_dates_says_so_and_keeps_state_clean(session, master_profile, test_user):
    service = Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)  # рабочих дней нет вообще
    await session.commit()
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=service.id))
        assert h.tg.sent_to(CLIENT_TG) == ["К сожалению, свободных дат пока нет. Попробуйте позже."]
        assert await h.get_state(CLIENT_TG) is None


async def test_pressing_the_button_removes_the_whole_price_list_and_leaves_only_the_calendar(session, world):
    async with tg_harness(admin_content.router, client_booking.router) as h:
        _with_screen_cleanup(h)
        await h.send_text(CLIENT_TG, PRICE_BTN)
        cards = [_message_id(h, m) for m in _sent(h) if m.text.startswith(("Маникюр", "Брови"))]
        assert len(cards) == 2

        before = len(h.tg.requests)
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=world["brows_id"]))

        [calendar] = [m for m in h.tg.requests[before:] if isinstance(m, SendMessage)]
        assert calendar.text.startswith("Брови\n\n📅 Выберите дату:")
        deleted = _deleted_ids(h, before)
        assert set(cards) <= deleted  # убраны ВСЕ карточки прайса, не только та, под которой нажали
        assert _message_id(h, calendar) not in deleted  # календарь остался
        # Календарь показан до удаления: человек не видит пустой чат.
        first_delete = next(i for i, m in enumerate(h.tg.requests[before:]) if isinstance(m, (DeleteMessage, DeleteMessages)))
        assert h.tg.requests[before:].index(calendar) < first_delete


async def test_refused_press_leaves_the_price_list_in_place(session, world, test_user):
    await set_client(session, test_user, is_restricted=True)
    async with tg_harness(admin_content.router, client_booking.router) as h:
        _with_screen_cleanup(h)
        await h.send_text(CLIENT_TG, PRICE_BTN)
        before = len(h.tg.requests)
        await h.press(CLIENT_TG, ServiceBookCallback(service_id=world["manicure_id"]))
        assert h.tg.sent_to(CLIENT_TG)[-1] == client_booking.RESTRICTED_TEXT
        assert _deleted_ids(h, before) == set()  # прайс нужен клиенту и дальше: ничего не удалено
