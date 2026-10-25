"""Особые дни: исключение из расписания на дату главнее расписания по дням недели.

Сценарий: мастер работает пн–пт, а одну субботу открывает; или закрывает один рабочий день. Проверяем всю цепочку:
свободное время и запись, календарь клиента, блокировку времени, статистику и сам сценарий мастера («🕐 Расписание → 📌 Изменить график конкретного дня»).
"""

import calendar
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.methods import DeleteMessage, EditMessageText

from app.bot.calendar_view import has_working_days
from app.bot.handlers.admin import block_time, special_days
from app.bot.handlers.admin.schedule import _weekday_keyboard
from app.bot.handlers.client import check_time as client_check_time
from app.bot.keyboards.callback_data import (
    AdminScheduleCallback,
    CalendarCallback,
    ConfirmCallback,
    DateCallback,
    NavCallback,
    TimeCallback,
)
from app.bot.states.booking_states import AdminBlockTimeFSM, AdminSpecialDayFSM, CheckTimeFSM
from app.database.models import Booking, BookingStatus, Master, Service, WorkingDayOverride, WorkingHours
from app.database.repositories.schedule_repository import ScheduleRepository
from app.services.booking_service import BookingService, SlotNotAvailableError
from app.services.stats_service import calculate_period_stats
from app.tenancy import TenantContext
from app.utils.timezone import get_master_tz
from tests.conftest import MASTER_ID, next_weekday, set_owner
from tests.tg import tg_harness

ADMIN_ID = 999
CLIENT_TG = 111111


@pytest_asyncio.fixture
async def world(session, master_profile, test_user):
    """Пн–пт рабочие 10–19, суббота и воскресенье — выходные; услуга 60 минут."""
    await set_owner(session, ADMIN_ID)
    for weekday in range(5):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    for weekday in (5, 6):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=False))
    service = Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)
    await session.commit()
    tz = await get_master_tz(session, MASTER_ID)
    saturday = next_weekday(5, 30)
    return SimpleNamespace(
        tz=tz,
        service=service,
        user=test_user,
        saturday=saturday,
        other_saturday=saturday + dt.timedelta(days=7),
        monday=next_weekday(0, 30),
        svc=BookingService(session, tz, master_id=MASTER_ID),
        repo=ScheduleRepository(session, MASTER_ID),
    )


def _at(w, day: dt.date, hour: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour), tzinfo=w.tz)


def _cell(markup, day: dt.date):
    weeks = calendar.monthcalendar(day.year, day.month)
    week_index = next(i for i, week in enumerate(weeks) if day.day in week)
    return markup.inline_keyboard[2 + week_index][day.weekday()]


def _last_edit(h) -> EditMessageText:
    return [m for m in h.tg.requests if isinstance(m, EditMessageText)][-1]


async def _open_month(h, tg_id: int, day: dt.date):
    """Нажимает переход на месяц этой даты и возвращает разметку обновлённого календаря."""
    await h.press(tg_id, CalendarCallback(action="month", year=day.year, month=day.month))
    return _last_edit(h).reply_markup


async def _override(session, day: dt.date):
    return await ScheduleRepository(session, MASTER_ID).get_override(day)


# ---------- свободное время и запись ----------


async def test_open_one_saturday_only_that_saturday_is_bookable(session, world):
    await world.repo.set_override(world.saturday, True, dt.time(10), dt.time(14))
    await session.commit()

    slots = await world.svc.get_available_slots(world.saturday, 60)
    assert [s.astimezone(world.tz).strftime("%H:%M") for s in slots] == ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00"]
    assert await world.svc.get_available_slots(world.other_saturday, 60) == []

    after = world.saturday + dt.timedelta(days=2)  # понедельник
    dates = await world.svc.dates_with_slots([world.saturday, world.other_saturday, after], 60)
    assert dates == [world.saturday, after]


async def test_booking_is_possible_on_the_opened_day_and_not_on_other_saturdays(session, world):
    await world.repo.set_override(world.saturday, True, dt.time(10), dt.time(14))
    await session.commit()

    booking = await world.svc.create_booking(world.user.id, world.service.id, _at(world, world.saturday, 11))
    assert booking.status == BookingStatus.CONFIRMED.value
    with pytest.raises(SlotNotAvailableError):
        await world.svc.create_booking(world.user.id, world.service.id, _at(world, world.other_saturday, 11))


async def test_close_one_working_day_only_that_day_is_closed(session, world):
    await world.repo.set_override(world.monday, False, None, None)
    await session.commit()

    assert await world.svc.get_available_slots(world.monday, 60) == []
    assert await world.svc.get_available_slots(world.monday + dt.timedelta(days=7), 60) != []
    assert await world.svc.dates_with_slots([world.monday, world.monday + dt.timedelta(days=1)], 60) == [world.monday + dt.timedelta(days=1)]


async def test_reset_returns_the_day_to_the_weekly_schedule(session, world):
    await world.repo.set_override(world.saturday, True, dt.time(10), dt.time(14))
    await session.commit()
    assert await world.repo.delete_override(world.saturday) is True
    await session.commit()
    assert await world.svc.get_available_slots(world.saturday, 60) == []


async def test_override_changes_hours_of_a_working_day(session, world):
    await world.repo.set_override(world.monday, True, dt.time(14), dt.time(16))
    await session.commit()
    slots = await world.svc.get_available_slots(world.monday, 60)
    assert [s.astimezone(world.tz).strftime("%H:%M") for s in slots] == ["14:00", "14:30", "15:00"]


async def test_has_working_days_counts_open_special_days_inside_the_window(session, master_profile):
    tz = await get_master_tz(session, MASTER_ID)
    tenant = TenantContext(master_id=MASTER_ID, master=await session.get(Master, MASTER_ID), session=session)
    assert await has_working_days(session, tenant, tz) is False  # расписания нет вовсе

    repo = ScheduleRepository(session, MASTER_ID)
    today = dt.datetime.now(tz).date()
    await repo.set_override(today + dt.timedelta(days=400), True, dt.time(10), dt.time(12))  # за пределами окна записи
    await session.commit()
    assert await has_working_days(session, tenant, tz) is False

    await repo.set_override(today + dt.timedelta(days=20), True, dt.time(10), dt.time(12))
    await session.commit()
    assert await has_working_days(session, tenant, tz) is True


# ---------- статистика ----------


def test_stats_free_minutes_use_the_override_hours():
    day = dt.date(2030, 1, 19)  # суббота
    tz = dt.timezone(dt.timedelta(hours=2))
    override = WorkingDayOverride(master_id=1, day=day, is_working=True, start_time=dt.time(10), end_time=dt.time(12))
    common = dict(
        bookings=[], start_date=day, end_date=day, tz=tz, now=dt.datetime(2030, 1, 1, tzinfo=tz),
        working_hours_by_weekday={}, breaks_by_weekday={}, blocked_periods=[],
    )
    assert calculate_period_stats(**common).free_minutes == 0
    assert calculate_period_stats(**common, working_hours_overrides={day: override}).free_minutes == 120


# ---------- календарь клиента и блокировка времени ----------


async def test_client_calendar_shows_only_the_opened_saturday_as_free(session, world):
    await world.repo.set_override(world.saturday, True, dt.time(10), dt.time(14))
    await world.repo.set_override(world.monday, False, None, None)
    await session.commit()
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        opened = _cell(await _open_month(h, CLIENT_TG, world.saturday), world.saturday)
        assert opened.callback_data == DateCallback(date=world.saturday.isoformat()).pack()
        assert _cell(await _open_month(h, CLIENT_TG, world.other_saturday), world.other_saturday).text == "➖"
        # закрытый мастером особо день выглядит как занятый, а выходной по расписанию остаётся «➖»
        assert _cell(await _open_month(h, CLIENT_TG, world.monday), world.monday).text == "🔴"


async def test_block_time_works_on_the_opened_day(session, world):
    await world.repo.set_override(world.saturday, True, dt.time(10), dt.time(14))
    await session.commit()
    async with tg_harness(block_time.router) as h:
        await h.set_state(ADMIN_ID, AdminBlockTimeFSM.select_date)
        await h.press(ADMIN_ID, DateCallback(date=world.saturday.isoformat()))
        assert await h.get_state(ADMIN_ID) == AdminBlockTimeFSM.select_start_time.state


# ---------- сценарий мастера ----------


def test_schedule_screen_has_the_special_days_button():
    last_row = _weekday_keyboard().inline_keyboard[-1]
    assert [(b.text, b.callback_data) for b in last_row] == [("📌 Изменить график конкретного дня", "sd:open")]


async def test_master_opens_one_saturday(session, world):
    async with tg_harness(special_days.router) as h:
        await h.press(ADMIN_ID, "sd:open")
        assert await h.get_state(ADMIN_ID) == AdminSpecialDayFSM.select_date.state
        assert "Изменений графика пока нет." in _last_edit(h).text

        await h.press(ADMIN_ID, DateCallback(date=world.saturday.isoformat()))
        assert await h.get_state(ADMIN_ID) == AdminSpecialDayFSM.day_menu.state
        assert "По расписанию: выходной" in _last_edit(h).text

        await h.press(ADMIN_ID, AdminScheduleCallback(action="sd_open"))
        await h.press(ADMIN_ID, TimeCallback(ts=int(_at(world, world.saturday, 10).timestamp())))
        await h.press(ADMIN_ID, TimeCallback(ts=int(_at(world, world.saturday, 14).timestamp())))
        assert await h.get_state(ADMIN_ID) == AdminSpecialDayFSM.confirm.state
        assert "10:00 — 14:00" in _last_edit(h).text

        await h.press(ADMIN_ID, ConfirmCallback(action="yes"))
        assert await h.get_state(ADMIN_ID) is None
        assert _last_edit(h).text.startswith("✅")

    saved = await _override(session, world.saturday)
    assert (saved.is_working, saved.start_time, saved.end_time) == (True, dt.time(10), dt.time(14))
    assert await world.svc.get_available_slots(world.saturday, 60) != []


async def test_calendar_marks_special_days_and_lists_them(session, world):
    await world.repo.set_override(world.saturday, True, dt.time(10), dt.time(14))
    await world.repo.set_override(world.monday, False, None, None)
    await session.commit()
    async with tg_harness(special_days.router) as h:
        await h.set_state(ADMIN_ID, AdminSpecialDayFSM.select_date)
        saturday_month = await _open_month(h, ADMIN_ID, world.saturday)
        text = _last_edit(h).text
        monday_month = await _open_month(h, ADMIN_ID, world.monday)
    assert "Ближайшие изменения графика:" in text
    assert f"✅ Сб {world.saturday.strftime('%d.%m')}: 10:00 — 14:00" in text
    assert f"🔴 Пн {world.monday.strftime('%d.%m')}: выходной" in text
    assert _cell(saturday_month, world.saturday).text == "✅"  # открытый особый день: без числа, как и закрытый
    closed = _cell(monday_month, world.monday)
    assert closed.text == "🔴"  # как занятый день: без числа, но кликабелен — день можно открыть или вернуть по расписанию
    assert closed.callback_data == DateCallback(date=world.monday.isoformat()).pack()


async def test_master_closes_a_free_day_right_away(session, world):
    async with tg_harness(special_days.router) as h:
        await h.set_state(ADMIN_ID, AdminSpecialDayFSM.select_date)
        await h.press(ADMIN_ID, DateCallback(date=world.monday.isoformat()))
        await h.press(ADMIN_ID, AdminScheduleCallback(action="sd_close"))
        assert await h.get_state(ADMIN_ID) is None
        assert "день закрыт" in _last_edit(h).text

    saved = await _override(session, world.monday)
    assert saved.is_working is False and saved.start_time is None
    assert await world.svc.get_available_slots(world.monday, 60) == []


async def test_closing_a_day_with_a_booking_asks_for_confirmation(session, world):
    booking = await world.svc.create_booking(world.user.id, world.service.id, _at(world, world.monday, 11))
    await session.commit()
    async with tg_harness(special_days.router) as h:
        await h.set_state(ADMIN_ID, AdminSpecialDayFSM.select_date)
        await h.press(ADMIN_ID, DateCallback(date=world.monday.isoformat()))
        await h.press(ADMIN_ID, AdminScheduleCallback(action="sd_close"))
        assert await h.get_state(ADMIN_ID) == AdminSpecialDayFSM.confirm_close.state
        assert "уже есть подтверждённые записи" in _last_edit(h).text
        assert await _override(session, world.monday) is None  # пока ничего не закрыто

        await h.press(ADMIN_ID, ConfirmCallback(action="edit"))  # «Изменить» — назад к экрану дня
        assert await h.get_state(ADMIN_ID) == AdminSpecialDayFSM.day_menu.state
        await h.press(ADMIN_ID, AdminScheduleCallback(action="sd_close"))
        await h.press(ADMIN_ID, ConfirmCallback(action="yes"))
        assert await h.get_state(ADMIN_ID) is None

    assert (await _override(session, world.monday)).is_working is False
    kept = await session.get(Booking, booking.id)
    assert kept.status == BookingStatus.CONFIRMED.value  # запись не отменяется сама


async def test_reset_removes_the_override(session, world):
    await world.repo.set_override(world.saturday, True, dt.time(10), dt.time(14))
    await session.commit()
    async with tg_harness(special_days.router) as h:
        await h.set_state(ADMIN_ID, AdminSpecialDayFSM.select_date)
        await h.press(ADMIN_ID, DateCallback(date=world.saturday.isoformat()))
        assert "Изменено на этот день: 10:00 — 14:00" in _last_edit(h).text
        await h.press(ADMIN_ID, AdminScheduleCallback(action="sd_reset"))
        assert await h.get_state(ADMIN_ID) is None
        assert "снова по расписанию (выходной)" in _last_edit(h).text
    assert await _override(session, world.saturday) is None


async def test_dates_outside_the_booking_window_are_rejected(session, world):
    yesterday = dt.datetime.now(world.tz).date() - dt.timedelta(days=1)
    async with tg_harness(special_days.router) as h:
        await h.set_state(ADMIN_ID, AdminSpecialDayFSM.select_date)
        await h.press(ADMIN_ID, DateCallback(date=yesterday.isoformat()))
        await h.press(ADMIN_ID, DateCallback(date="not-a-date"))
        assert h.tg.alerts() == ["Эта дата недоступна. Выберите другую."] * 2
        assert await h.get_state(ADMIN_ID) == AdminSpecialDayFSM.select_date.state
    assert await _override(session, yesterday) is None


async def test_back_and_cancel_navigation(session, world):
    async with tg_harness(special_days.router) as h:
        await h.set_state(ADMIN_ID, AdminSpecialDayFSM.select_date)
        await h.press(ADMIN_ID, DateCallback(date=world.saturday.isoformat()))
        await h.press(ADMIN_ID, NavCallback(target="back"))
        assert await h.get_state(ADMIN_ID) == AdminSpecialDayFSM.select_date.state
        await h.press(ADMIN_ID, CalendarCallback(action="cancel"))
        assert await h.get_state(ADMIN_ID) is None
        assert [m for m in h.tg.requests if isinstance(m, DeleteMessage)]
    assert await _override(session, world.saturday) is None
