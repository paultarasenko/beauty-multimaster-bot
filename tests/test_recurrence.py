import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.database.models import Recurrence
from app.utils.recurrence import (
    describe_schedule,
    is_date_too_far,
    next_occurrence,
    parse_date_input,
    parse_time_input,
)

MINSK = ZoneInfo("Europe/Minsk")  # UTC+3 без перехода на летнее время
UTC = dt.timezone.utc
T = dt.time(19, 30)


def utc(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


def local(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=MINSK)


def nxt(recurrence, anchor: dt.date, after: dt.datetime, time: dt.time = T, tz=MINSK) -> dt.datetime | None:
    return next_occurrence(recurrence, anchor, time, tz, after)


# --- Разбор ввода ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("230986", dt.date(1986, 9, 23)),
        ("231026", dt.date(2026, 10, 23)),
        ("23.10.26", dt.date(2026, 10, 23)),
        ("  231026 ", dt.date(2026, 10, 23)),
        ("290224", dt.date(2024, 2, 29)),
    ],
)
def test_parse_date_ok(raw, expected):
    assert parse_date_input(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "23102", "2310266", "321026", "311326", "300226", "290225", "23-10-2026"])
def test_parse_date_rejects_garbage_and_impossible_dates(raw):
    assert parse_date_input(raw) is None


@pytest.mark.parametrize("raw, expected", [("1930", dt.time(19, 30)), ("0005", dt.time(0, 5)), ("19:30", dt.time(19, 30))])
def test_parse_time_ok(raw, expected):
    assert parse_time_input(raw) == expected


@pytest.mark.parametrize("raw", ["", "930", "19300", "2460", "2500", "ab12"])
def test_parse_time_rejects_garbage_and_impossible_times(raw):
    assert parse_time_input(raw) is None


# --- Защита от дат «из будущего» ---


def test_date_120_years_ahead_or_more_is_too_far():
    today = dt.date(2026, 10, 2)
    assert not is_date_too_far(dt.date(2146, 10, 1), today)  # 119 лет и 364 дня
    assert is_date_too_far(dt.date(2146, 10, 2), today)  # ровно 120 лет
    assert is_date_too_far(dt.date(2300, 1, 1), today)
    assert not is_date_too_far(dt.date(2026, 10, 2), today)
    assert not is_date_too_far(dt.date(1986, 9, 23), today)  # прошлое здесь не проверяется


def test_too_far_limit_on_feb_29():
    today = dt.date(2080, 2, 29)  # через 120 лет, в 2200-м, 29 февраля нет — граница 28 февраля
    assert is_date_too_far(dt.date(2200, 2, 28), today)
    assert not is_date_too_far(dt.date(2200, 2, 27), today)


# --- ONCE ---


def test_once_in_future_fires_at_local_wall_clock_in_utc():
    result = nxt(Recurrence.ONCE, dt.date(2026, 10, 5), after=utc(2026, 10, 2, 9, 0))
    assert result == utc(2026, 10, 5, 16, 30)  # 19:30 по Минску = 16:30 UTC
    assert result.tzinfo == UTC


def test_once_in_past_has_no_occurrence():
    assert nxt(Recurrence.ONCE, dt.date(2026, 10, 1), after=utc(2026, 10, 2, 9, 0)) is None


def test_once_exactly_now_is_already_past():
    assert nxt(Recurrence.ONCE, dt.date(2026, 10, 2), after=utc(2026, 10, 2, 16, 30)) is None


# --- DAILY ---


def test_daily_today_if_time_not_yet_passed():
    assert nxt(Recurrence.DAILY, dt.date(2026, 10, 2), after=utc(2026, 10, 2, 9, 0)) == utc(2026, 10, 2, 16, 30)


def test_daily_tomorrow_if_time_passed():
    assert nxt(Recurrence.DAILY, dt.date(2026, 10, 2), after=utc(2026, 10, 2, 17, 0)) == utc(2026, 10, 3, 16, 30)


def test_daily_future_start_waits_for_start_date():
    assert nxt(Recurrence.DAILY, dt.date(2026, 10, 10), after=utc(2026, 10, 2, 9, 0)) == utc(2026, 10, 10, 16, 30)


def test_daily_past_start_rolls_forward():
    assert nxt(Recurrence.DAILY, dt.date(2020, 1, 1), after=utc(2026, 10, 2, 17, 0)) == utc(2026, 10, 3, 16, 30)


def test_daily_after_long_downtime_gives_single_next_not_catch_up():
    fired = utc(2026, 10, 2, 16, 30)
    assert nxt(Recurrence.DAILY, dt.date(2026, 10, 2), after=utc(2026, 10, 9, 8, 0)) == utc(2026, 10, 9, 16, 30)
    assert fired < utc(2026, 10, 9, 16, 30)


def test_local_midnight_crosses_utc_date():
    # 00:30 по Минску 5 октября — это 21:30 UTC 4 октября.
    assert nxt(Recurrence.ONCE, dt.date(2026, 10, 5), after=utc(2026, 10, 1), time=dt.time(0, 30)) == utc(2026, 10, 4, 21, 30)


# --- WEEKLY ---


def test_weekly_keeps_weekday_of_anchor():
    anchor = dt.date(2026, 10, 5)  # понедельник
    first = nxt(Recurrence.WEEKLY, anchor, after=utc(2026, 10, 1))
    assert first == utc(2026, 10, 5, 16, 30)
    assert nxt(Recurrence.WEEKLY, anchor, after=first) == utc(2026, 10, 12, 16, 30)


def test_weekly_past_anchor_lands_on_same_weekday():
    anchor = dt.date(2026, 9, 23)  # среда
    result = nxt(Recurrence.WEEKLY, anchor, after=utc(2026, 10, 2, 9, 0))  # пятница
    assert result == utc(2026, 10, 7, 16, 30)
    assert result.astimezone(MINSK).weekday() == anchor.weekday()


# --- MONTHLY ---


def test_monthly_next_month_after_firing():
    anchor = dt.date(2026, 10, 15)
    assert nxt(Recurrence.MONTHLY, anchor, after=utc(2026, 10, 15, 16, 30)) == utc(2026, 11, 15, 16, 30)


def test_monthly_year_rollover():
    assert nxt(Recurrence.MONTHLY, dt.date(2026, 12, 15), after=utc(2026, 12, 15, 16, 30)) == utc(2027, 1, 15, 16, 30)


def test_monthly_day_31_clamps_to_last_day_and_recovers():
    anchor = dt.date(2026, 1, 31)
    feb = nxt(Recurrence.MONTHLY, anchor, after=utc(2026, 1, 31, 16, 30))
    assert feb == utc(2026, 2, 28, 16, 30)
    assert nxt(Recurrence.MONTHLY, anchor, after=feb) == utc(2026, 3, 31, 16, 30)
    assert nxt(Recurrence.MONTHLY, anchor, after=utc(2026, 4, 1)) == utc(2026, 4, 30, 16, 30)


def test_monthly_future_anchor_not_earlier_than_anchor():
    assert nxt(Recurrence.MONTHLY, dt.date(2026, 12, 5), after=utc(2026, 10, 2)) == utc(2026, 12, 5, 16, 30)


# --- YEARLY («Каждый год») ---


def test_yearly_birthday_style_date_from_the_past_rolls_to_next_occurrence():
    # Пример из ТЗ: 23.09.1986 — срабатывает каждый год 23 сентября.
    assert nxt(Recurrence.YEARLY, dt.date(1986, 9, 23), after=utc(2026, 10, 2)) == utc(2027, 9, 23, 16, 30)
    assert nxt(Recurrence.YEARLY, dt.date(1986, 9, 23), after=utc(2026, 9, 1)) == utc(2026, 9, 23, 16, 30)


def test_yearly_feb_29_clamps_in_non_leap_years():
    anchor = dt.date(2024, 2, 29)
    assert nxt(Recurrence.YEARLY, anchor, after=utc(2025, 1, 1)) == utc(2025, 2, 28, 16, 30)
    assert nxt(Recurrence.YEARLY, anchor, after=utc(2027, 3, 1)) == utc(2028, 2, 29, 16, 30)


# --- Летнее время: настенные часы не «плывут» ---


def test_wall_clock_time_is_stable_across_dst_change():
    riga = ZoneInfo("Europe/Riga")  # переход на зимнее время 25 октября 2026
    anchor = dt.date(2026, 10, 24)
    first = next_occurrence(Recurrence.DAILY, anchor, T, riga, utc(2026, 10, 24, 0, 0))
    second = next_occurrence(Recurrence.DAILY, anchor, T, riga, first)
    third = next_occurrence(Recurrence.DAILY, anchor, T, riga, second)
    assert [x.astimezone(riga).strftime("%H:%M") for x in (first, second, third)] == ["19:30"] * 3
    assert second - first == dt.timedelta(hours=25)


# --- Подписи ---


def test_describe_schedule():
    assert describe_schedule(Recurrence.ONCE, dt.date(2026, 10, 5), T) == "Один раз: 5 октября 2026, 19:30"
    assert describe_schedule(Recurrence.DAILY, dt.date(2026, 10, 5), T) == "Каждый день в 19:30"
    assert describe_schedule(Recurrence.WEEKLY, dt.date(2026, 10, 5), T) == "Раз в неделю: по понедельникам в 19:30"
    assert describe_schedule(Recurrence.MONTHLY, dt.date(2026, 10, 15), T) == "Раз в месяц: 15 числа в 19:30"
    assert "в последний день" in describe_schedule(Recurrence.MONTHLY, dt.date(2026, 10, 31), T)
    assert describe_schedule(Recurrence.YEARLY, dt.date(1986, 9, 23), T) == "Каждый год: 23 сентября в 19:30"
