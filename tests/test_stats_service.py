"""Статистика мастера: границы периодов, доход (факт/ожидаемый) и свободные часы."""

import datetime as dt
from decimal import Decimal

import pytest

from app.database.models import BlockedPeriod, Booking, BreakPeriod, WorkingHours
from app.services.stats_service import (
    PERIOD_3MONTHS,
    PERIOD_6MONTHS,
    PERIOD_MONTH,
    PERIOD_TODAY,
    PERIOD_WEEK,
    calculate_period_stats,
    get_free_hours_end_date,
    get_period_dates,
    get_period_stats,
)
from tests.conftest import MASTER_ID, RIGA_TZ

D = dt.date
WED = D(2030, 1, 16)  # среда; неделя — 14–20 января 2030


def local(hour: int, minute: int = 0, day: dt.date = WED) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=RIGA_TZ)


# --- границы периодов ---


@pytest.mark.parametrize(
    ("period", "today", "expected"),
    [
        (PERIOD_TODAY, WED, (WED, WED)),
        (PERIOD_WEEK, WED, (D(2030, 1, 14), D(2030, 1, 20))),
        (PERIOD_WEEK, D(2030, 1, 14), (D(2030, 1, 14), D(2030, 1, 20))),  # понедельник — начало недели
        (PERIOD_WEEK, D(2030, 1, 20), (D(2030, 1, 14), D(2030, 1, 20))),  # воскресенье — ещё та же неделя
        (PERIOD_WEEK, D(2030, 1, 1), (D(2029, 12, 31), D(2030, 1, 6))),  # неделя на стыке годов
        (PERIOD_MONTH, WED, (D(2030, 1, 1), D(2030, 1, 31))),
        (PERIOD_MONTH, D(2028, 2, 10), (D(2028, 2, 1), D(2028, 2, 29))),  # високосный год
        (PERIOD_MONTH, D(2030, 2, 10), (D(2030, 2, 1), D(2030, 2, 28))),
        (PERIOD_MONTH, D(2030, 12, 31), (D(2030, 12, 1), D(2030, 12, 31))),
        (PERIOD_3MONTHS, WED, (WED, D(2030, 4, 15))),  # 90 дней включительно
        (PERIOD_6MONTHS, WED, (WED, D(2030, 7, 14))),  # 180 дней включительно
    ],
)
def test_period_dates(period, today, expected):
    assert get_period_dates(period, today) == expected


def test_unknown_period_raises():
    with pytest.raises(ValueError):
        get_period_dates("year", WED)


def test_free_hours_end_date_only_extends_for_month():
    assert get_free_hours_end_date(PERIOD_MONTH, WED, D(2030, 1, 31)) == D(2030, 1, 31)
    assert get_free_hours_end_date(PERIOD_MONTH, D(2028, 2, 10), D(2028, 2, 29)) == D(2028, 2, 29)
    for period in (PERIOD_TODAY, PERIOD_WEEK, PERIOD_3MONTHS, PERIOD_6MONTHS):
        assert get_free_hours_end_date(period, WED, D(2030, 1, 20)) == D(2030, 1, 20)


# --- чистый расчёт ---


def booking(start: dt.datetime, hours: float = 1, status: str = "confirmed", price: str = "40") -> Booking:
    return Booking(master_id=MASTER_ID,
        start_datetime=start,
        end_datetime=start + dt.timedelta(hours=hours),
        status=status,
        service_price_snapshot=Decimal(price),
    )


def working(weekday: int = WED.weekday(), start=dt.time(10), end=dt.time(19), is_working=True) -> WorkingHours:
    return WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=is_working, start_time=start, end_time=end)


def stats(bookings=(), *, now, hours=None, breaks=None, blocked=(), end_date=WED, free_end_date=None):
    return calculate_period_stats(
        bookings=list(bookings),
        start_date=WED,
        end_date=end_date,
        tz=RIGA_TZ,
        now=now,
        working_hours_by_weekday=hours if hours is not None else {WED.weekday(): working()},
        breaks_by_weekday=breaks if breaks is not None else {},
        blocked_periods=list(blocked),
        free_end_date=free_end_date,
    )


def test_free_minutes_full_working_day_before_it_starts():
    assert stats(now=local(8)).free_minutes == 9 * 60


def test_free_minutes_subtract_break_booking_and_block():
    breaks = {WED.weekday(): [BreakPeriod(master_id=MASTER_ID, weekday=WED.weekday(), start_time=dt.time(13), end_time=dt.time(14))]}
    blocked = [BlockedPeriod(master_id=MASTER_ID, start_datetime=local(16), end_datetime=local(17))]
    result = stats([booking(local(11))], now=local(8), breaks=breaks, blocked=blocked)
    assert result.free_minutes == (9 - 1 - 1 - 1) * 60


def test_free_minutes_count_only_time_from_now_onwards():
    assert stats(now=local(15)).free_minutes == 4 * 60
    assert stats(now=local(15, 30)).free_minutes == 3 * 60 + 30
    assert stats(now=local(19)).free_minutes == 0
    assert stats(now=local(23)).free_minutes == 0


def test_free_minutes_zero_on_day_off_or_missing_schedule():
    assert stats(now=local(8), hours={WED.weekday(): working(is_working=False)}).free_minutes == 0
    assert stats(now=local(8), hours={WED.weekday(): None}).free_minutes == 0
    assert stats(now=local(8), hours={}).free_minutes == 0


def test_revenue_actual_vs_expected():
    bookings = [
        booking(local(10), status="completed", price="40.50"),  # выполнена
        booking(local(11), status="confirmed", price="40"),  # закончилась, но воркер ещё не перевёл в completed
        booking(local(14, 30), status="confirmed", price="25"),  # идёт прямо сейчас — это ещё «ожидаемый»
        booking(local(17), status="confirmed", price="30"),  # впереди
    ]
    result = stats(bookings, now=local(15))
    assert result.bookings_count == 4
    assert result.actual_revenue == Decimal("80.50")
    assert result.expected_revenue == Decimal("135.50")


def test_booking_ending_exactly_now_is_already_earned():
    result = stats([booking(local(14))], now=local(15))
    assert result.actual_revenue == Decimal("40")


def test_period_boundaries_start_inclusive_end_exclusive():
    midnight = local(0)
    next_midnight = local(0, day=WED + dt.timedelta(days=1))
    prev_evening = local(23, day=WED - dt.timedelta(days=1))
    result = stats([booking(midnight, 0.5), booking(next_midnight, 0.5), booking(prev_evening, 0.5)], now=local(12))
    assert result.bookings_count == 1


def test_booking_from_previous_day_overlapping_today_is_busy_but_not_counted():
    night = booking(local(22, day=WED - dt.timedelta(days=1)), hours=14)  # до 12:00 сегодня
    result = stats([night], now=local(8))
    assert result.bookings_count == 0
    assert result.free_minutes == 7 * 60  # 12:00–19:00


def test_free_end_date_extends_free_hours_beyond_period_but_not_counts():
    hours = {d: working(weekday=d) for d in range(7)}
    thursday = WED + dt.timedelta(days=1)
    result = stats([booking(local(11, day=thursday))], now=local(8), hours=hours, free_end_date=thursday)
    assert result.bookings_count == 0  # четверг — не в периоде «сегодня»
    assert result.free_minutes == 9 * 60 + 8 * 60  # сегодня целиком + четверг без часа записи


def test_money_is_exact_decimal():
    result = stats([booking(local(10), price="0.10"), booking(local(11), price="0.20")], now=local(8))
    assert result.expected_revenue == Decimal("0.30")


# --- с БД ---


@pytest.fixture
async def schedule(session, master_profile):
    session.add_all(working(weekday=d) for d in range(7))
    await session.commit()


async def _add(session, user, service, start, status="confirmed", price=40):
    session.add(
        Booking(master_id=MASTER_ID,
            user_id=user.id,
            service_id=service.id,
            start_datetime=start,
            end_datetime=start + dt.timedelta(hours=1),
            status=status,
            service_name_snapshot=service.name,
            service_price_snapshot=price,
            service_duration_snapshot=60,
        )
    )
    await session.commit()


async def test_db_stats_include_only_confirmed_and_completed(session, schedule, test_user, manicure_service):
    await _add(session, test_user, manicure_service, local(10), "completed", 40)
    await _add(session, test_user, manicure_service, local(11), "confirmed", 50)
    await _add(session, test_user, manicure_service, local(12), "cancelled", 60)
    await _add(session, test_user, manicure_service, local(13), "no_show", 70)
    await _add(session, test_user, manicure_service, local(10, day=WED + dt.timedelta(days=1)), "confirmed", 80)

    result, start, end = await get_period_stats(session, MASTER_ID, RIGA_TZ, PERIOD_TODAY, local(8))

    assert (start, end) == (WED, WED)
    assert result.bookings_count == 2
    assert result.expected_revenue == Decimal("90.00")
    assert result.actual_revenue == Decimal("40.00")
    # занято только подтверждённой записью 11–12; отменённая и «не пришёл» время не занимают
    assert result.free_minutes == (9 - 1) * 60


async def test_db_month_free_hours_run_to_end_of_month_but_revenue_stays_in_period(
    session, schedule, test_user, manicure_service
):
    await _add(session, test_user, manicure_service, local(10, day=D(2030, 1, 28)), "confirmed", 55)

    week, _, _ = await get_period_stats(session, MASTER_ID, RIGA_TZ, PERIOD_WEEK, local(8))
    month, _, _ = await get_period_stats(session, MASTER_ID, RIGA_TZ, PERIOD_MONTH, local(8))

    assert week.bookings_count == 0 and week.expected_revenue == 0  # 28-е в следующей неделе
    assert month.bookings_count == 1 and month.expected_revenue == Decimal("55.00")
    # 16–31 января = 16 дней по 9 часов, минус час записи
    assert month.free_minutes == (16 * 9 - 1) * 60


async def test_db_booking_spanning_period_start_blocks_time(session, schedule, test_user, manicure_service):
    await _add(session, test_user, manicure_service, local(9, 30), "confirmed", 40)  # 9:30–10:30
    result, _, _ = await get_period_stats(session, MASTER_ID, RIGA_TZ, PERIOD_TODAY, local(8))
    assert result.free_minutes == 8 * 60 + 30  # 10:30–19:00
