"""«Проверить время» -> «Записаться» -> «Другой день» / «Этот день»: настоящий диспетчер и БД, Telegram — запись запросов."""

import datetime as dt

import pytest
import pytest_asyncio
from aiogram.methods import EditMessageReplyMarkup, SendPhoto

from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import check_time as client_check_time
from app.bot.keyboards.callback_data import DateCallback, NavCallback, ScheduleBookCallback, ServiceCallback
from app.bot.states.booking_states import BookingFSM, CheckTimeFSM
from app.database.models import BlockedPeriod, Service, WorkingHours
from app.services.booking_service import BookingService
from tests.conftest import MASTER_ID, RIGA_TZ, next_weekday
from tests.tg import tg_harness

CLIENT_TG = 111111  # test_user


@pytest_asyncio.fixture
async def world(session, master_profile, test_user):
    """Рабочий день (пятница) через 2+ недели с записью на 15:00 и ещё один рабочий день (четверг)."""
    day = next_weekday(4)
    other_day = next_weekday(3)
    for d in (day, other_day):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=d.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    service = Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)
    await session.commit()
    start = dt.datetime.combine(day, dt.time(15, 0), tzinfo=RIGA_TZ)
    await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(test_user.id, service.id, start)
    await session.commit()
    return {"day": day, "other_day": other_day, "service_id": service.id}


def _keyboards(h, method):
    return [m.reply_markup for m in h.tg.requests if isinstance(m, method) and m.reply_markup is not None]


async def test_schedule_photo_button_carries_the_viewed_date(session, world):
    day = world["day"]
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        await h.press(CLIENT_TG, DateCallback(date=day.isoformat()))
    [kb] = _keyboards(h, SendPhoto)
    [[button]] = kb.inline_keyboard
    assert button.text == "📅 Записаться"
    assert button.callback_data == ScheduleBookCallback(action="ask", date=day.isoformat()).pack()


async def test_booking_button_is_replaced_by_two_buttons(session, world):
    day = world["day"]
    async with tg_harness(client_check_time.router) as h:
        await h.press(CLIENT_TG, ScheduleBookCallback(action="ask", date=day.isoformat()))
    [kb] = _keyboards(h, EditMessageReplyMarkup)
    [row] = kb.inline_keyboard
    assert [b.text for b in row] == ["Другой день", "Этот день"]  # слева — другой день, справа — этот
    assert row[0].callback_data == NavCallback(target="start_booking").pack()
    assert row[1].callback_data == ScheduleBookCallback(action="day", date=day.isoformat()).pack()


async def test_this_day_skips_date_selection_and_shows_times(session, world):
    day = world["day"]
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ScheduleBookCallback(action="day", date=day.isoformat()))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_service.state
        assert h.tg.sent_to(CLIENT_TG)[-1] == f"📅 {day.strftime('%d.%m.%Y')}\n\nВыберите услугу"

        await h.press(CLIENT_TG, ServiceCallback(service_id=world["service_id"]))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_time.state
        shown = h.tg.edited()[-1]
        assert f"📅 {day.strftime('%d.%m.%Y')}" in shown and "🕐 Свободное время:" in shown
        data = await h.fsm(CLIENT_TG).get_data()
        assert data["selected_date"] == day.isoformat() and data["preselected_date"] is None


async def test_other_day_runs_the_usual_flow_with_date_selection(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, NavCallback(target="start_booking"))
        assert h.tg.sent_to(CLIENT_TG)[-1] == "Выберите услугу"
        await h.press(CLIENT_TG, ServiceCallback(service_id=world["service_id"]))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_date.state
        assert "📅 Выберите дату:" in h.tg.edited()[-1]


async def test_other_day_forgets_the_day_picked_earlier(session, world):
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ScheduleBookCallback(action="day", date=world["day"].isoformat()))
        await h.press(CLIENT_TG, NavCallback(target="start_booking"))
        assert (await h.fsm(CLIENT_TG).get_data())["preselected_date"] is None
        await h.press(CLIENT_TG, ServiceCallback(service_id=world["service_id"]))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_date.state


async def test_this_day_without_free_time_falls_back_to_date_selection(session, world):
    day = world["day"]
    session.add(
        BlockedPeriod(master_id=MASTER_ID,
            start_datetime=dt.datetime.combine(day, dt.time(0, 0), tzinfo=RIGA_TZ),
            end_datetime=dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=RIGA_TZ),
        )
    )
    await session.commit()
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ScheduleBookCallback(action="day", date=day.isoformat()))
        await h.press(CLIENT_TG, ServiceCallback(service_id=world["service_id"]))
        assert await h.get_state(CLIENT_TG) == BookingFSM.select_date.state
        assert h.tg.edited()[-1].startswith(
            f"На {day.strftime('%d.%m.%Y')} для этой услуги уже нет свободного времени.\n\n"
        )
        assert "📅 Выберите дату:" in h.tg.edited()[-1]


async def test_this_day_in_the_past_is_refused(session, world):
    yesterday = dt.date.today() - dt.timedelta(days=2)
    async with tg_harness(client_booking.router) as h:
        await h.press(CLIENT_TG, ScheduleBookCallback(action="day", date=yesterday.isoformat()))
        assert await h.get_state(CLIENT_TG) is None
        assert h.tg.alerts() == ["Эта дата уже прошла. Нажмите «Другой день»."]
        assert h.tg.sent_to(CLIENT_TG) == []


@pytest.mark.parametrize("action", ["ask", "day"])
async def test_garbage_date_in_callback_is_ignored(session, world, action):
    async with tg_harness(client_check_time.router, client_booking.router) as h:
        await h.press(CLIENT_TG, ScheduleBookCallback(action=action, date="not-a-date"))
        assert await h.get_state(CLIENT_TG) is None
        assert h.tg.sent_to(CLIENT_TG) == []
