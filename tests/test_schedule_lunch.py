"""Обед мастера: повторяющийся перерыв внутри рабочего дня (экран «🕐 Расписание» → день → «🍽 Обед»)."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.admin import schedule as admin_schedule
from app.bot.keyboards.callback_data import AdminScheduleCallback, ConfirmCallback, NavCallback, TimeCallback
from app.bot.states.booking_states import AdminScheduleFSM
from app.database.models import BookingStatus, BreakPeriod, WorkingHours
from app.database.repositories.schedule_repository import ScheduleRepository
from app.services.booking_service import BookingService
from tests.conftest import MASTER_ID, next_weekday, set_owner
from tests.tg import tg_harness

MASTER_TZ = ZoneInfo("Europe/Riga")  # таймзона из master_profile; хендлер переводит timestamp кнопки в неё

ADMIN_ID = 999
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY = 0, 1, 2, 3, 4


@pytest_asyncio.fixture(autouse=True)
async def _admin(session):
    await set_owner(session, ADMIN_ID)


@pytest.fixture
async def week(session, master_profile):
    """Пн–Ср 10:00–19:00, Чт — короткий день 10:00–13:30, Пт — выходной, остальных дней в БД нет."""
    repo = ScheduleRepository(session, MASTER_ID)
    for day in (MONDAY, TUESDAY, WEDNESDAY):
        await repo.upsert_working_hours(day, is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0))
    await repo.upsert_working_hours(THURSDAY, is_working=True, start_time=dt.time(10, 0), end_time=dt.time(13, 30))
    await repo.upsert_working_hours(FRIDAY, is_working=False, start_time=None, end_time=None)
    await session.commit()


def _ts(hour: int, minute: int = 0) -> int:
    return int(dt.datetime.combine(dt.datetime.now(MASTER_TZ).date(), dt.time(hour, minute), tzinfo=MASTER_TZ).timestamp())


def _last(h) -> str:
    return h.tg.edited()[-1]


async def _breaks(session) -> dict[int, tuple[dt.time, dt.time]]:
    session.expire_all()
    rows = (await session.execute(select(BreakPeriod).where(BreakPeriod.master_id == MASTER_ID))).scalars().all()
    return {r.weekday: (r.start_time, r.end_time) for r in rows}


async def _pick_lunch(h, weekday: int, start: tuple[int, int] = (13, 0), end: tuple[int, int] = (14, 0)) -> None:
    """Расписание → день → «Обед» → «Задать обед» → начало → конец (до экрана выбора «куда сохранить»)."""
    await h.press(ADMIN_ID, f"wd:{weekday}")
    await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch"))
    await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_set"))
    await h.press(ADMIN_ID, TimeCallback(ts=_ts(*start)))
    await h.press(ADMIN_ID, TimeCallback(ts=_ts(*end)))


async def test_lunch_for_one_day_is_saved_only_for_that_day(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await _pick_lunch(h, MONDAY)
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.lunch_confirm.state
        assert "Понедельник: 13:00 — 14:00" in _last(h)

        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_day"))

        assert _last(h) == "✅ Понедельник: обед 13:00 — 14:00 сохранён."
        assert await h.get_state(ADMIN_ID) is None
    assert await _breaks(session) == {MONDAY: (dt.time(13), dt.time(14))}


async def test_lunch_for_all_working_days_skips_days_where_it_does_not_fit(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await _pick_lunch(h, MONDAY)

        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_all"))

        # Чт работает только до 13:30 — обед 13:00–14:00 туда не помещается; Пт — выходной, про него молчим.
        assert _last(h) == "✅ Обед 13:00 — 14:00 сохранён: Пн, Вт, Ср.\nНе подошёл по рабочим часам: Чт."
    assert await _breaks(session) == {d: (dt.time(13), dt.time(14)) for d in (MONDAY, TUESDAY, WEDNESDAY)}


async def test_saving_lunch_replaces_the_previous_one(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await _pick_lunch(h, MONDAY, (12, 0), (13, 0))
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_day"))
        await _pick_lunch(h, MONDAY, (14, 0), (15, 0))
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_day"))

    assert await _breaks(session) == {MONDAY: (dt.time(14), dt.time(15))}  # один обед в день, а не два


async def test_lunch_screen_shows_current_lunch_and_offers_removal_only_when_it_exists(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch"))
        assert _last(h) == "Понедельник\n🕐 Работа: 10:00 — 19:00\n🍽 Обед: не задан"
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.lunch_menu.state

        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_remove"))  # убирать нечего — но это не ошибка
        assert _last(h) == "✅ Понедельник: обед убран."


async def test_remove_lunch(session, week):
    repo = ScheduleRepository(session, MASTER_ID)
    await repo.set_lunch(MONDAY, dt.time(13), dt.time(14))
    await repo.set_lunch(TUESDAY, dt.time(13), dt.time(14))
    await session.commit()

    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch"))
        assert "🍽 Обед: 13:00 — 14:00" in _last(h)

        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_remove"))

        assert _last(h) == "✅ Понедельник: обед убран."
    assert await _breaks(session) == {TUESDAY: (dt.time(13), dt.time(14))}  # другой день не тронут


async def test_lunch_cannot_be_opened_for_a_day_off(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{FRIDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch"))

        assert h.tg.alerts() == ["Сначала задайте часы работы этого дня: обед должен быть внутри них."]
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.select_start_time.state


async def test_forged_lunch_time_outside_working_hours_is_rejected(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch"))
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_set"))

        await h.press(ADMIN_ID, TimeCallback(ts=_ts(8, 0)))  # до начала рабочего дня

        assert h.tg.alerts() == ["Это время недоступно. Выберите другое."]
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.lunch_start.state

        await h.press(ADMIN_ID, TimeCallback(ts=_ts(13, 0)))
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(20, 0)))  # после конца рабочего дня

        assert h.tg.alerts()[-1] == "Это время недоступно. Выберите другое."
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.lunch_end.state
    assert await _breaks(session) == {}


async def test_lunch_end_must_be_after_start(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch"))
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_set"))
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(13, 0)))

        await h.press(ADMIN_ID, TimeCallback(ts=_ts(12, 0)))

        assert h.tg.alerts() == ["Это время недоступно. Выберите другое."]
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.lunch_end.state


async def test_back_buttons_walk_up_one_screen_at_a_time(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await _pick_lunch(h, MONDAY)
        await h.press(ADMIN_ID, ConfirmCallback(action="edit"))
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.lunch_start.state

        await h.press(ADMIN_ID, NavCallback(target="back"))
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.lunch_menu.state

        await h.press(ADMIN_ID, NavCallback(target="back"))
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.select_start_time.state
        assert _last(h).startswith("Понедельник.\n🕐 Сейчас: 10:00 — 19:00")
    assert await _breaks(session) == {}  # ничего не сохранено без подтверждения


async def test_week_list_and_day_screen_show_lunch(session, week):
    repo = ScheduleRepository(session, MASTER_ID)
    await repo.set_lunch(MONDAY, dt.time(13), dt.time(14))
    await session.commit()

    async with tg_harness(admin_schedule.router) as h:
        await h.send_text(ADMIN_ID, "🕐 Расписание")
        week_list = h.tg.sent_to(ADMIN_ID)[-1]
        await h.press(ADMIN_ID, f"wd:{MONDAY}")

        assert "Понедельник: 10:00 — 19:00 · 🍽 13:00 — 14:00" in week_list
        assert "Вторник: 10:00 — 19:00\n" in week_list  # у дня без обеда строка прежняя
        assert _last(h) == (
            "Понедельник.\n🕐 Сейчас: 10:00 — 19:00\n🍽 Обед: 13:00 — 14:00\n\n"
            "Выберите время начала работы, или сделайте день выходным"
        )


async def test_making_a_day_off_removes_its_lunch(session, week):
    repo = ScheduleRepository(session, MASTER_ID)
    await repo.set_lunch(MONDAY, dt.time(13), dt.time(14))
    await repo.set_lunch(TUESDAY, dt.time(13), dt.time(14))
    await session.commit()

    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, AdminScheduleCallback(action="day_off"))

    assert await _breaks(session) == {TUESDAY: (dt.time(13), dt.time(14))}


async def test_new_working_hours_keep_a_lunch_that_still_fits(session, week):
    await ScheduleRepository(session, MASTER_ID).set_lunch(MONDAY, dt.time(13), dt.time(14))
    await session.commit()

    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(9, 0)))
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(18, 0)))
        await h.press(ADMIN_ID, ConfirmCallback(action="yes"))

        assert _last(h) == "✅ Понедельник: 09:00 — 18:00 сохранено."
    assert await _breaks(session) == {MONDAY: (dt.time(13), dt.time(14))}


async def test_new_working_hours_drop_a_lunch_that_no_longer_fits_and_say_so(session, week):
    await ScheduleRepository(session, MASTER_ID).set_lunch(MONDAY, dt.time(13), dt.time(14))
    await session.commit()

    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(9, 0)))
        await h.press(ADMIN_ID, TimeCallback(ts=_ts(12, 0)))
        await h.press(ADMIN_ID, ConfirmCallback(action="yes"))

        assert _last(h) == (
            "✅ Понедельник: 09:00 — 12:00 сохранено.\n"
            "🍽 Обед не помещался в новые часы работы, поэтому убран. Задайте его заново в «🍽 Обед»."
        )
    assert await _breaks(session) == {}


async def test_clients_get_no_slots_during_lunch_and_service_cannot_straddle_it(session, week, manicure_service):
    """Главное, ради чего обед нужен: слоты клиента. Услуга 60 минут, сетка по 30, рабочий день 10:00–19:00, обед 13:00–14:00."""
    day = next_weekday(MONDAY)
    await ScheduleRepository(session, MASTER_ID).set_lunch(MONDAY, dt.time(13), dt.time(14))
    await session.commit()

    slots = await BookingService(session, MASTER_TZ, master_id=MASTER_ID).get_available_slots(day, 60)
    starts = {s.astimezone(MASTER_TZ).time() for s in slots}

    assert dt.time(12, 0) in starts  # 12:00–13:00 — ровно до обеда
    assert not starts & {dt.time(12, 30), dt.time(13, 0), dt.time(13, 30)}  # любое из них пересекает обед
    assert dt.time(14, 0) in starts  # сразу после обеда


async def test_existing_bookings_at_lunch_time_are_kept_and_master_is_warned(session, week, test_user, manicure_service):
    day = next_weekday(MONDAY)
    service = BookingService(session, MASTER_TZ, master_id=MASTER_ID)
    in_lunch = await service.create_booking(
        test_user.id, manicure_service.id, dt.datetime.combine(day, dt.time(13, 0), tzinfo=MASTER_TZ)
    )
    before = await service.create_booking(
        test_user.id, manicure_service.id, dt.datetime.combine(day, dt.time(11, 0), tzinfo=MASTER_TZ)
    )
    await session.commit()

    async with tg_harness(admin_schedule.router) as h:
        await _pick_lunch(h, MONDAY)
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_day"))

        text = _last(h)
        assert text.startswith("✅ Понедельник: обед 13:00 — 14:00 сохранён.")
        assert "⚠️ На это время уже есть подтверждённые записи:" in text
        assert f"{day.strftime('%d.%m')} 13:00 — Маникюр" in text
        assert f"{day.strftime('%d.%m')} 11:00" not in text  # запись до обеда в предупреждение не попадает
    session.expire_all()
    await session.refresh(in_lunch)
    await session.refresh(before)
    assert in_lunch.status == before.status == BookingStatus.CONFIRMED.value  # ничего не отменено автоматически
    assert await _breaks(session) == {MONDAY: (dt.time(13), dt.time(14))}


async def test_no_warning_when_nobody_is_booked_at_lunch_time(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await _pick_lunch(h, MONDAY)
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_day"))

        assert "⚠️" not in _last(h)


async def test_lunch_screens_are_closed_to_non_owners(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await h.press(ADMIN_ID, f"wd:{MONDAY}")
        await h.press(ADMIN_ID + 1, AdminScheduleCallback(action="lunch"))  # чужой человек подделал кнопку

        assert await h.get_state(ADMIN_ID + 1) is None
        assert await h.get_state(ADMIN_ID) == AdminScheduleFSM.select_start_time.state


async def test_working_hours_rows_are_not_touched_by_lunch(session, week):
    async with tg_harness(admin_schedule.router) as h:
        await _pick_lunch(h, MONDAY)
        await h.press(ADMIN_ID, AdminScheduleCallback(action="lunch_all"))

    session.expire_all()
    rows = (await session.execute(select(WorkingHours).where(WorkingHours.master_id == MASTER_ID))).scalars().all()
    hours = {r.weekday: (r.is_working, r.start_time, r.end_time) for r in rows}
    assert hours[MONDAY] == (True, dt.time(10), dt.time(19))
    assert hours[THURSDAY] == (True, dt.time(10), dt.time(13, 30))
    assert hours[FRIDAY][0] is False
