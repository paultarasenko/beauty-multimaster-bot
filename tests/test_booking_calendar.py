"""Выбор даты в записи, переносе и добавлении записи мастером: календарь вместо сетки кнопок с датами.

Занятые для выбранной услуги дни — 🔴, выходные — ➖; «Назад» из выбора времени возвращает в календарь на месяц выбранной даты.
Настоящий диспетчер и БД, Telegram — запись запросов.
"""

import calendar
import datetime as dt
from types import SimpleNamespace

import pytest_asyncio
from aiogram.methods import DeleteMessage, EditMessageText

from app.bot.handlers.admin import add_booking
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import my_booking, reminder_actions
from app.bot.keyboards.callback_data import (
    BookingActionCallback,
    CalendarCallback,
    DateCallback,
    DatesPageCallback,
    NavCallback,
    ReminderActionCallback,
    ServiceCallback,
)
from app.bot.calendar_view import calendar_text
from app.bot.keyboards.client_keyboards import calendar_title
from app.bot.states.booking_states import AdminAddBookingFSM, BookingFSM, RescheduleFSM
from app.database.models import BlockedPeriod, Service, WorkingHours
from app.services.booking_service import BookingService
from app.database.repositories.schedule_repository import ScheduleRepository
from app.utils.timezone import get_master_tz
from tests.conftest import MASTER_ID, next_weekday, set_owner
from tests.tg import tg_harness

ADMIN_ID = 999
CLIENT_TG = 111111  # test_user
SERVICE_NAME = "Маникюр"


@pytest_asyncio.fixture
async def world(session, master_profile, test_user):
    """Пн–пт 10–19, сб–вс выходные; услуга 60 минут и тяжёлая услуга 10 часов (не помещается ни в один день)."""
    await set_owner(session, ADMIN_ID)
    for weekday in range(5):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    for weekday in (5, 6):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=False))
    service = Service(master_id=MASTER_ID, name=SERVICE_NAME, price=40, duration_minutes=60, active=True, sort_order=0)
    huge = Service(master_id=MASTER_ID, name="Марафон", price=400, duration_minutes=600, active=True, sort_order=1)
    session.add_all([service, huge])
    await session.commit()
    tz = await get_master_tz(session, MASTER_ID)
    return SimpleNamespace(
        tz=tz,
        service=service,
        huge=huge,
        user=test_user,
        free=next_weekday(1, 30),
        saturday=next_weekday(5, 30),
        short=next_weekday(2, 40),  # окно 30 минут: свободно для 30-минутной услуги, но не для часовой
        closed=next_weekday(3, 30),
        repo=ScheduleRepository(session, MASTER_ID),
        svc=BookingService(session, tz, master_id=MASTER_ID),
    )


def _cell(markup, day: dt.date):
    weeks = calendar.monthcalendar(day.year, day.month)
    week_index = next(i for i, week in enumerate(weeks) if day.day in week)
    return markup.inline_keyboard[2 + week_index][day.weekday()]


def _footer(markup) -> list[str]:
    return [button.text for button in markup.inline_keyboard[-1]]


def _title(markup) -> str:
    return markup.inline_keyboard[0][1].text


def _last_edit(h) -> EditMessageText:
    return [m for m in h.tg.requests if isinstance(m, EditMessageText)][-1]


async def _open_month(h, tg_id: int, day: dt.date):
    await h.press(tg_id, CalendarCallback(action="month", year=day.year, month=day.month))
    return _last_edit(h).reply_markup


async def _close_day_and_short_day(world, session):
    """`closed` — день целиком закрыт блокировкой времени (🔴); `short` — особый день с окном 30 минут."""
    session.add(
        BlockedPeriod(
            master_id=MASTER_ID,
            start_datetime=dt.datetime.combine(world.closed, dt.time(0, 0), tzinfo=world.tz),
            end_datetime=dt.datetime.combine(world.closed + dt.timedelta(days=1), dt.time(0, 0), tzinfo=world.tz),
        )
    )
    await world.repo.set_override(world.short, True, dt.time(10), dt.time(10, 30))
    await session.commit()


# ---------- запись клиента ----------


async def test_choosing_a_service_opens_the_calendar_marked_for_that_service(session, world):
    await _close_day_and_short_day(world, session)
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_service)
        await h.press(CLIENT_TG, ServiceCallback(service_id=world.service.id))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_date.state
        edit = _last_edit(h)
        assert edit.text == calendar_text(f"{SERVICE_NAME}\n\n📅 Выберите дату:")
        assert _footer(edit.reply_markup) == ["✖ Отмена"]
        today = dt.datetime.now(world.tz).date()
        assert _title(edit.reply_markup) == calendar_title(today.year, today.month)

        free_month = await _open_month(h, CLIENT_TG, world.free)
        assert _cell(free_month, world.free).callback_data == DateCallback(date=world.free.isoformat()).pack()
        assert _cell(await _open_month(h, CLIENT_TG, world.saturday), world.saturday).text == "➖"
        assert _cell(await _open_month(h, CLIENT_TG, world.closed), world.closed).text == "🔴"
        assert _cell(await _open_month(h, CLIENT_TG, world.short), world.short).text == "🔴"  # 30 минут < 60


async def test_the_same_day_is_free_for_a_shorter_service(session, world):
    await _close_day_and_short_day(world, session)
    shorter = Service(master_id=MASTER_ID, name="Экспресс", price=10, duration_minutes=30, active=True, sort_order=2)
    session.add(shorter)
    await session.commit()
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_service)
        await h.press(CLIENT_TG, ServiceCallback(service_id=shorter.id))
        month = await _open_month(h, CLIENT_TG, world.short)
        assert _cell(month, world.short).callback_data == DateCallback(date=world.short.isoformat()).pack()


async def test_no_free_dates_for_the_service_shows_a_message_instead_of_the_calendar(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_service)
        await h.press(CLIENT_TG, ServiceCallback(service_id=world.huge.id))
        assert _last_edit(h).text == "К сожалению, свободных дат пока нет. Попробуйте позже."
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_service.state


async def test_popups_for_busy_and_off_days(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_date, service_id=world.service.id)
        await h.press(CLIENT_TG, CalendarCallback(action="busy"))
        await h.press(CLIENT_TG, CalendarCallback(action="off"))
        assert h.tg.alerts() == ["На этот день нет свободного времени", "Этот день выходной"]


async def test_back_from_time_selection_returns_to_the_calendar_of_that_month(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_service)
        await h.press(CLIENT_TG, ServiceCallback(service_id=world.service.id))
        await h.press(CLIENT_TG, DateCallback(date=world.free.isoformat()))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_time.state

        await h.press(CLIENT_TG, NavCallback(target="back"))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_date.state
        edit = _last_edit(h)
        assert _title(edit.reply_markup) == calendar_title(world.free.year, world.free.month)
        assert edit.text == calendar_text(f"{SERVICE_NAME}\n\n📅 Выберите дату:")


async def test_old_grid_buttons_open_the_calendar_and_cancel_closes_it(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_service)
        await h.press(CLIENT_TG, ServiceCallback(service_id=world.service.id))
        await h.press(CLIENT_TG, DatesPageCallback(page=1))  # кнопка из старого сообщения с сеткой дат
        assert _footer(_last_edit(h).reply_markup) == ["✖ Отмена"]

        await h.press(CLIENT_TG, CalendarCallback(action="cancel"))
        assert await h.get_state(CLIENT_TG) is None
        assert [m for m in h.tg.requests if isinstance(m, DeleteMessage)]


async def test_forged_month_is_ignored_in_booking(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.set_state(CLIENT_TG, BookingFSM.select_date, service_id=world.service.id)
        await h.press(CLIENT_TG, CalendarCallback(action="month", year=0, month=13))
        assert not [m for m in h.tg.requests if isinstance(m, EditMessageText)]


# ---------- перенос записи клиентом ----------


async def _booking(session, world):
    booking = await world.svc.create_booking(
        world.user.id, world.service.id, dt.datetime.combine(world.free, dt.time(12), tzinfo=world.tz)
    )
    await session.commit()
    return booking


async def test_reschedule_from_my_bookings_opens_the_calendar(session, world):
    booking = await _booking(session, world)
    async with tg_harness(my_booking.router) as h:
        await h.press(CLIENT_TG, BookingActionCallback(action="reschedule", booking_id=booking.id))
        assert await h.get_state(CLIENT_TG) == RescheduleFSM.select_date.state
        edit = _last_edit(h)
        assert edit.text == calendar_text("📅 Выберите новую дату:")
        assert _footer(edit.reply_markup) == ["✖ Отмена"]
        assert _cell(await _open_month(h, CLIENT_TG, world.saturday), world.saturday).text == "➖"


async def test_reschedule_back_from_time_and_cancel(session, world):
    booking = await _booking(session, world)
    other = world.free + dt.timedelta(days=7)
    async with tg_harness(my_booking.router) as h:
        await h.press(CLIENT_TG, BookingActionCallback(action="reschedule", booking_id=booking.id))
        await h.press(CLIENT_TG, DateCallback(date=other.isoformat()))
        assert await h.get_state(CLIENT_TG) == RescheduleFSM.select_time.state

        await h.press(CLIENT_TG, NavCallback(target="back"))
        assert await h.get_state(CLIENT_TG) == RescheduleFSM.select_date.state
        assert _title(_last_edit(h).reply_markup) == calendar_title(other.year, other.month)

        await h.press(CLIENT_TG, CalendarCallback(action="cancel"))  # отказ от переноса — карточка записи
        assert await h.get_state(CLIENT_TG) is None
        assert SERVICE_NAME in _last_edit(h).text


async def test_reschedule_from_a_reminder_opens_the_same_calendar(session, world):
    booking = await _booking(session, world)
    async with tg_harness(reminder_actions.router) as h:
        await h.press(CLIENT_TG, ReminderActionCallback(action="reschedule", booking_id=booking.id))
        assert await h.get_state(CLIENT_TG) == RescheduleFSM.select_date.state
        assert _last_edit(h).text == calendar_text("📅 Выберите новую дату:")


# ---------- запись, которую добавляет мастер ----------


async def test_admin_add_booking_calendar_has_back_to_services_and_cancel(session, world):
    async with tg_harness(add_booking.router) as h:
        await h.set_state(ADMIN_ID, AdminAddBookingFSM.select_service)
        await h.press(ADMIN_ID, ServiceCallback(service_id=world.service.id))
        assert await h.get_state(ADMIN_ID) == AdminAddBookingFSM.select_date.state
        edit = _last_edit(h)
        assert edit.text == calendar_text("📅 Выберите дату:")
        assert _footer(edit.reply_markup) == ["⬅️ Назад", "✖ Отмена"]
        assert _cell(await _open_month(h, ADMIN_ID, world.saturday), world.saturday).text == "➖"

        await h.press(ADMIN_ID, NavCallback(target="back"))
        assert await h.get_state(ADMIN_ID) == AdminAddBookingFSM.select_service.state


async def test_admin_back_from_time_returns_to_that_month_and_cancel_closes(session, world):
    async with tg_harness(add_booking.router) as h:
        await h.set_state(ADMIN_ID, AdminAddBookingFSM.select_service)
        await h.press(ADMIN_ID, ServiceCallback(service_id=world.service.id))
        await h.press(ADMIN_ID, DateCallback(date=world.free.isoformat()))
        assert await h.get_state(ADMIN_ID) == AdminAddBookingFSM.select_time.state

        await h.press(ADMIN_ID, NavCallback(target="back"))
        assert await h.get_state(ADMIN_ID) == AdminAddBookingFSM.select_date.state
        assert _title(_last_edit(h).reply_markup) == calendar_title(world.free.year, world.free.month)

        await h.press(ADMIN_ID, CalendarCallback(action="cancel"))
        assert await h.get_state(ADMIN_ID) is None
        assert [m for m in h.tg.requests if isinstance(m, DeleteMessage)]


async def test_admin_no_free_dates_message(session, world):
    async with tg_harness(add_booking.router) as h:
        await h.set_state(ADMIN_ID, AdminAddBookingFSM.select_service)
        await h.press(ADMIN_ID, ServiceCallback(service_id=world.huge.id))
        assert _last_edit(h).text == "Свободных дат нет."
