import datetime as dt

from app.database.models import BlockedPeriod, BreakPeriod, WorkingHours
from app.services.availability_service import AvailabilityService, Interval
from tests.conftest import MASTER_ID

TZ = dt.timezone(dt.timedelta(hours=2))
DATE = dt.date(2026, 10, 16)


def wh(is_working=True, start="10:00", end="19:00"):
    return WorkingHours(master_id=MASTER_ID,
        weekday=DATE.weekday(),
        is_working=is_working,
        start_time=dt.time.fromisoformat(start) if start else None,
        end_time=dt.time.fromisoformat(end) if end else None,
    )


def at(hhmm: str) -> dt.datetime:
    return dt.datetime.combine(DATE, dt.time.fromisoformat(hhmm), tzinfo=TZ)


def test_day_off_has_no_slots():
    svc = AvailabilityService(slot_interval_minutes=30)
    slots = svc.get_available_slots(DATE, TZ, 60, wh(is_working=False), [], [], [])
    assert slots == []


def test_simple_working_day_60min_service():
    svc = AvailabilityService(slot_interval_minutes=30)
    slots = svc.get_available_slots(DATE, TZ, 60, wh(), [], [], [])
    # 10:00-19:00, шаг 30 мин, услуга 60 мин -> последний слот в 18:00
    assert slots[0] == at("10:00")
    assert slots[-1] == at("18:00")
    assert at("18:30") not in slots  # 18:30+60=19:30 не влезает


def test_break_splits_free_interval():
    svc = AvailabilityService(slot_interval_minutes=30)
    breaks = [BreakPeriod(master_id=MASTER_ID, weekday=DATE.weekday(), start_time=dt.time(14, 0), end_time=dt.time(15, 0))]
    slots = svc.get_available_slots(DATE, TZ, 60, wh(), breaks, [], [])
    # Не должно быть слота, пересекающего перерыв 14:00-15:00
    assert at("13:30") not in slots  # 13:30+60=14:30 залезает в перерыв
    assert at("14:00") not in slots
    assert at("14:30") not in slots
    assert at("13:00") in slots  # 13:00-14:00 влезает точно до перерыва
    assert at("15:00") in slots  # сразу после перерыва


def test_blocked_period_removes_slots():
    svc = AvailabilityService(slot_interval_minutes=30)
    blocked = [BlockedPeriod(master_id=MASTER_ID, start_datetime=at("14:00"), end_datetime=at("17:00"))]
    slots = svc.get_available_slots(DATE, TZ, 60, wh(), [], blocked, [])
    assert at("14:00") not in slots
    assert at("16:00") not in slots
    assert at("13:00") in slots
    assert at("17:00") in slots


def test_existing_booking_splits_interval_middle_of_day():
    svc = AvailabilityService(slot_interval_minutes=30)
    booking_ranges = [Interval(at("13:00"), at("14:30"))]
    slots = svc.get_available_slots(DATE, TZ, 60, wh(), [], [], booking_ranges)
    assert at("13:00") not in slots
    assert at("13:30") not in slots  # 13:30+60=14:30 пересекается с концом booking ровно по границе -> ok на самом деле
    # проверим явно границу: 14:30 свободно (booking заканчивается в 14:30, услуга начинается в 14:30 - не пересекается)
    assert at("14:30") in slots
    assert at("12:00") in slots


def test_long_service_does_not_fit_at_end_of_day():
    """Ключевой edge case из ТЗ: если осталось 17:00-18:00 (60 минут), 90-минутная услуга
    не должна предлагаться, хотя формально слот 'начинается' в рабочее время."""
    svc = AvailabilityService(slot_interval_minutes=30)
    # Рабочий день 10:00-18:00, всё занято booking'ами кроме 17:00-18:00
    booking_ranges = [Interval(at("10:00"), at("17:00"))]
    slots = svc.get_available_slots(DATE, TZ, 90, wh(start="10:00", end="18:00"), [], [], booking_ranges)
    assert slots == []  # 90 минут никуда не помещается


def test_service_duration_30_60_90_all_correct():
    svc = AvailabilityService(slot_interval_minutes=30)
    for duration, expected_last in [(30, "18:30"), (60, "18:00"), (90, "17:30")]:
        slots = svc.get_available_slots(DATE, TZ, duration, wh(), [], [], [])
        assert slots[-1] == at(expected_last), f"failed for duration={duration}"


def test_slot_at_boundary_of_working_hours():
    svc = AvailabilityService(slot_interval_minutes=30)
    slots = svc.get_available_slots(DATE, TZ, 60, wh(start="10:00", end="11:00"), [], [], [])
    assert slots == [at("10:00")]  # ровно один слот, влезающий тик-в-тик


def test_multiple_bookings_leave_gaps():
    svc = AvailabilityService(slot_interval_minutes=30)
    booking_ranges = [
        Interval(at("10:00"), at("11:00")),
        Interval(at("13:00"), at("14:00")),
        Interval(at("17:00"), at("19:00")),
    ]
    slots = svc.get_available_slots(DATE, TZ, 30, wh(), [], [], booking_ranges)
    assert at("11:00") in slots
    assert at("14:00") in slots
    assert at("10:30") not in slots
    assert at("17:30") not in slots


def test_now_filter_excludes_past_slots():
    svc = AvailabilityService(slot_interval_minutes=30)
    now = at("14:00")
    slots = svc.get_available_slots(DATE, TZ, 60, wh(), [], [], [], now=now)
    assert at("13:00") not in slots
    assert at("14:00") in slots
    assert at("15:00") in slots


def test_has_any_slot_true_and_false():
    svc = AvailabilityService(slot_interval_minutes=30)
    assert svc.has_any_slot(DATE, TZ, 60, wh(), [], [], []) is True
    assert svc.has_any_slot(DATE, TZ, 60, wh(is_working=False), [], [], []) is False


def test_slot_offered_right_after_booking_end_in_master_tz():
    """Услуга 40 минут с 10:00 -> следующая запись доступна с 10:40, дальше — по сетке.
    Границы занятых интервалов приходят из БД в UTC — слоты должны быть в таймзоне мастера."""
    svc = AvailabilityService(slot_interval_minutes=30)
    booking = Interval(at("10:00").astimezone(dt.timezone.utc), at("10:40").astimezone(dt.timezone.utc))
    slots = svc.get_available_slots(DATE, TZ, 40, wh(), [], [], [booking])
    assert [s.strftime("%H:%M") for s in slots[:3]] == ["10:40", "11:00", "11:30"]
    assert all(s.utcoffset() == TZ.utcoffset(None) for s in slots)  # таймзона мастера, не UTC


def test_slots_after_blocked_period_from_db_are_in_master_tz():
    svc = AvailabilityService(slot_interval_minutes=30)
    blocked = [
        BlockedPeriod(master_id=MASTER_ID,
            start_datetime=at("16:00").astimezone(dt.timezone.utc),
            end_datetime=at("17:10").astimezone(dt.timezone.utc),
        )
    ]
    slots = svc.get_available_slots(DATE, TZ, 60, wh(), [], blocked, [])
    assert at("17:30") in slots
    assert at("17:10") not in slots
    assert all(s.utcoffset() == TZ.utcoffset(None) for s in slots)


def test_slots_after_break_ending_at_odd_time_stay_on_grid():
    svc = AvailabilityService(slot_interval_minutes=30)
    breaks = [BreakPeriod(master_id=MASTER_ID, weekday=DATE.weekday(), start_time=dt.time(14, 0), end_time=dt.time(14, 45))]
    slots = svc.get_available_slots(DATE, TZ, 60, wh(), breaks, [], [])
    assert at("14:45") not in slots
    assert at("15:00") in slots