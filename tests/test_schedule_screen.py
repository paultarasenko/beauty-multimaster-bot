"""Экран редактирования дня: под названием дня показаны сохранённые сейчас часы работы."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.admin import schedule as admin_schedule
from app.bot.keyboards.callback_data import AdminScheduleCallback, ConfirmCallback, NavCallback, TimeCallback
from app.bot.states.booking_states import AdminScheduleFSM
from app.database.models import WorkingHours
from app.database.repositories.schedule_repository import ScheduleRepository
from tests.conftest import MASTER_ID, RIGA_TZ, set_owner
from tests.tg import tg_harness

MASTER_TZ = ZoneInfo("Europe/Riga")  # таймзона из master_profile; хендлер переводит timestamp кнопки в неё

ADMIN_ID = 999
MONDAY, TUESDAY, WEDNESDAY = 0, 1, 2
PROMPT_TAIL = "Выберите время начала работы, или сделайте день выходным"


@pytest_asyncio.fixture(autouse=True)
async def _admin(session):
    await set_owner(session, ADMIN_ID)


@pytest.fixture
async def week(session, master_profile):
    repo = ScheduleRepository(session, MASTER_ID)
    await repo.upsert_working_hours(MONDAY, is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 30))
    await repo.upsert_working_hours(TUESDAY, is_working=False, start_time=None, end_time=None)
    await session.commit()  # среда — строки в БД нет вообще


def _ts(hour: int, minute: int = 0) -> int:
    return int(dt.datetime.combine(dt.datetime.now(MASTER_TZ).date(), dt.time(hour, minute), tzinfo=MASTER_TZ).timestamp())


def _last_screen(h) -> str:
    return h.tg.edited()[-1]


async def test_working_day_shows_current_start_and_end_under_the_day_name(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")

        assert _last_screen(h) == f"Понедельник.\n🕐 Сейчас: 10:00 — 19:30\n\n{PROMPT_TAIL}"
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.select_start_time.state


@pytest.mark.parametrize("weekday", [TUESDAY, WEDNESDAY], ids=["day-off", "no-row-yet"])
async def test_day_off_or_unconfigured_day_shows_day_off(session, week, weekday):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{weekday}")

        lines = _last_screen(h).splitlines()
        assert lines[1] == "🕐 Сейчас: выходной"


async def test_working_row_without_times_is_treated_as_day_off(session, master_profile):
    """Битая строка (is_working=True, но времени нет) не должна ронять экран и показывать «None — None»."""
    session.add(WorkingHours(master_id=MASTER_ID, weekday=MONDAY, is_working=True, start_time=None, end_time=None))
    await session.commit()
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        assert "Сейчас: выходной" in _last_screen(h)


async def test_current_hours_are_shown_again_after_back_from_end_time(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(12)))
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.select_end_time.state

        await h.press(ADMIN_ID, NavCallback(target="back"))

        assert "🕐 Сейчас: 10:00 — 19:30" in _last_screen(h), "незавершённый ввод (12:00) не должен подменять сохранённые часы"


async def test_current_hours_are_shown_again_after_edit_on_confirm_screen(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(12)))
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(17)))
        assert "Сохранить расписание?" in _last_screen(h)

        await h.press(ADMIN_ID, ConfirmCallback(action="edit"))

        assert "🕐 Сейчас: 10:00 — 19:30" in _last_screen(h)


async def test_current_hours_are_shown_after_cancelling_day_off_confirmation(session, week, test_user, manicure_service):
    """Если на этот день есть будущие записи, «Выходной» просит подтверждение; «Изменить» возвращает на экран дня."""
    from app.services.booking_service import BookingService
    from tests.conftest import next_weekday

    day = next_weekday(MONDAY)
    start = dt.datetime.combine(day, dt.time(11, 0), tzinfo=RIGA_TZ)
    await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(test_user.id, manicure_service.id, start)
    await session.commit()

    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="day_off"))
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.confirm_day_off.state

        await h.press(ADMIN_ID, ConfirmCallback(action="edit"))

        assert "🕐 Сейчас: 10:00 — 19:30" in _last_screen(h)
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.select_start_time.state


async def test_screen_shows_new_hours_after_saving_a_change(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(9, 30)))
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(18)))
        await h.press(ADMIN_ID, ConfirmCallback(action="yes"))
        assert "✅ Понедельник: 09:30 — 18:00 сохранено." in _last_screen(h)

        await h.press(ADMIN_ID, f"wd:{MONDAY}")

        assert "🕐 Сейчас: 09:30 — 18:00" in _last_screen(h)


async def test_screen_shows_day_off_right_after_making_day_off(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="day_off"))
        assert "теперь выходной" in _last_screen(h)

        await h.press(ADMIN_ID, f"wd:{MONDAY}")

        assert "🕐 Сейчас: выходной" in _last_screen(h)
    session.expire_all()
    row = (await session.execute(select(WorkingHours).where(WorkingHours.weekday == MONDAY))).scalar_one()
    assert row.is_working is False


async def test_week_list_and_day_screen_format_hours_identically(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.send_text(ADMIN_ID, "🕐 Расписание")
        week_list = h.tg.sent_to(ADMIN_ID)[-1]
        await h.press(ADMIN_ID, f"wd:{MONDAY}")

        assert "Понедельник: 10:00 — 19:30" in week_list
        assert "Вторник: выходной" in week_list and "Среда: выходной" in week_list
        assert "10:00 — 19:30" in _last_screen(h)
