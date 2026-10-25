"""«Закрыть время»: календарь на 6 месяцев вместо сетки дат. Настоящий диспетчер и БД, Telegram — запись запросов."""

import calendar
import datetime as dt

import pytest_asyncio
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage

from app.bot.handlers.admin import block_time
from app.bot.calendar_view import BOOKING_HORIZON_DAYS
from app.bot.keyboards.callback_data import CalendarCallback, DateCallback, NavCallback
from app.bot.calendar_view import calendar_text
from app.bot.keyboards.client_keyboards import CALENDAR_ARROW_BLANK, calendar_title
from app.bot.states.booking_states import AdminBlockTimeFSM
from app.database.models import BlockedPeriod, Service, WorkingHours
from app.utils.timezone import get_master_tz
from tests.conftest import MASTER_ID, RIGA_TZ, next_weekday, set_owner
from tests.tg import tg_harness

ADMIN_ID = 999
HEADER = "📅 Выберите дату блокировки"


@pytest_asyncio.fixture
async def world(session, master_profile):
    """Пн–Сб рабочие 10–19, воскресенье — выходной; среда через 30+ дней закрыта целиком."""
    await set_owner(session, ADMIN_ID)
    for weekday in range(6):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    session.add(Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0))
    closed = next_weekday(2, 30)
    session.add(
        BlockedPeriod(
            master_id=MASTER_ID,
            start_datetime=dt.datetime.combine(closed, dt.time(0, 0), tzinfo=RIGA_TZ),
            end_datetime=dt.datetime.combine(closed + dt.timedelta(days=1), dt.time(0, 0), tzinfo=RIGA_TZ),
        )
    )
    await session.commit()
    return {"closed": closed, "sunday": next_weekday(6, 30), "free": next_weekday(1, 30)}


def _cell(markup, day: dt.date):
    weeks = calendar.monthcalendar(day.year, day.month)
    week_index = next(i for i, week in enumerate(weeks) if day.day in week)
    return markup.inline_keyboard[2 + week_index][day.weekday()]


def _last_edit(h) -> EditMessageText:
    return [m for m in h.tg.requests if isinstance(m, EditMessageText)][-1]


async def _open_month(h, day: dt.date):
    await h.press(ADMIN_ID, CalendarCallback(action="month", year=day.year, month=day.month))
    return _last_edit(h).reply_markup


async def test_start_opens_calendar_and_keeps_the_date_step(session, world):
    tz = await get_master_tz(session, MASTER_ID)
    today = dt.datetime.now(tz).date()
    async with tg_harness(block_time.router) as h:
        await h.send_text(ADMIN_ID, "🚫 Закрыть время")
        assert await h.get_state(ADMIN_ID) == AdminBlockTimeFSM.select_date.state
        sent = [m for m in h.tg.requests if isinstance(m, SendMessage)][-1]
    assert sent.text == calendar_text(HEADER)
    assert sent.reply_markup.inline_keyboard[0][1].text == calendar_title(today.year, today.month)


async def test_closed_and_off_days_are_symbols_with_popups(session, world):
    async with tg_harness(block_time.router) as h:
        await h.set_state(ADMIN_ID, AdminBlockTimeFSM.select_date)
        assert _cell(await _open_month(h, world["closed"]), world["closed"]).text == "🔴"
        assert _cell(await _open_month(h, world["sunday"]), world["sunday"]).text == "➖"
        await h.press(ADMIN_ID, CalendarCallback(action="busy"))
        await h.press(ADMIN_ID, CalendarCallback(action="off"))
        assert h.tg.alerts() == ["На этот день нет свободного времени", "Этот день выходной"]


async def test_calendar_reaches_six_months_ahead_and_no_further(session, world):
    tz = await get_master_tz(session, MASTER_ID)
    last = dt.datetime.now(tz).date() + dt.timedelta(days=BOOKING_HORIZON_DAYS - 1)
    async with tg_harness(block_time.router) as h:
        await h.set_state(ADMIN_ID, AdminBlockTimeFSM.select_date)
        future = await _open_month(h, dt.date(2040, 1, 1))
    assert future.inline_keyboard[0][1].text == calendar_title(last.year, last.month)
    assert future.inline_keyboard[0][2].text == CALENDAR_ARROW_BLANK


async def test_free_day_goes_to_start_time_and_back_returns_to_that_month(session, world):
    day = world["free"]
    async with tg_harness(block_time.router) as h:
        await h.set_state(ADMIN_ID, AdminBlockTimeFSM.select_date)
        await h.press(ADMIN_ID, DateCallback(date=day.isoformat()))
        assert await h.get_state(ADMIN_ID) == AdminBlockTimeFSM.select_start_time.state

        await h.press(ADMIN_ID, NavCallback(target="back"))
        assert await h.get_state(ADMIN_ID) == AdminBlockTimeFSM.select_date.state
        edit = _last_edit(h)
        assert edit.text == calendar_text(HEADER)
        assert edit.reply_markup.inline_keyboard[0][1].text == calendar_title(day.year, day.month)


async def test_cancel_clears_state_and_deletes_calendar(session, world):
    async with tg_harness(block_time.router) as h:
        await h.set_state(ADMIN_ID, AdminBlockTimeFSM.select_date)
        await h.press(ADMIN_ID, CalendarCallback(action="cancel"))
        assert await h.get_state(ADMIN_ID) is None
        assert [m for m in h.tg.requests if isinstance(m, DeleteMessage)]


async def test_no_working_days_means_nothing_to_close(session, master_profile):
    await set_owner(session, ADMIN_ID)
    async with tg_harness(block_time.router) as h:
        await h.send_text(ADMIN_ID, "🚫 Закрыть время")
        assert h.tg.sent_to(ADMIN_ID) == ["У мастера нет рабочих дней — закрывать нечего."]
        assert await h.get_state(ADMIN_ID) is None
