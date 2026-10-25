import datetime as dt
import os
from types import SimpleNamespace

import pytest

from app.services.availability_service import AvailabilityService, Interval
from app.utils import schedule_image
from app.utils.schedule_image import _merge_rows, _slot_rows, render_day_schedule_image

TZ = dt.timezone(dt.timedelta(hours=3))
DATE = dt.date(2026, 10, 7)
START, END = dt.time(10, 0), dt.time(20, 0)


def at(hhmm: str) -> dt.datetime:
    hour, minute = map(int, hhmm.split(":"))
    return dt.datetime.combine(DATE, dt.time(hour, minute), tzinfo=TZ)


def booking(start: str, end: str) -> Interval:
    # Как в БД: границы записей приходят в UTC.
    return Interval(at(start).astimezone(dt.timezone.utc), at(end).astimezone(dt.timezone.utc))


def working_hours():
    return SimpleNamespace(is_working=True, start_time=START, end_time=END)


def free_for(bookings: list[Interval]) -> list[Interval]:
    return AvailabilityService(30).get_free_intervals(DATE, TZ, working_hours(), [], [], bookings)


def label(row) -> tuple[str, str, bool]:
    return row[0].strftime("%H:%M"), row[1].strftime("%H:%M"), row[2]


def test_row_is_split_at_booking_end_not_on_grid():
    """Запись до 14:20: строка 14:00–14:30 делится на занятую 14:00–14:20 и свободную 14:20–14:30."""
    rows = _slot_rows(DATE, TZ, START, END, free_for([booking("12:30", "14:20")]))
    labels = [label(r) for r in rows]

    assert ("14:00", "14:20", False) in labels
    assert ("14:20", "14:30", True) in labels
    assert ("14:00", "14:30", False) not in labels


def test_every_bookable_slot_start_is_shown_as_free_on_image():
    """Каждое время, которое предлагается клиенту для записи, не должно быть «Занято» на картинке."""
    bookings = [booking("12:30", "14:20"), booking("17:00", "18:00")]
    free = free_for(bookings)
    slots = AvailabilityService(30).get_available_slots(DATE, TZ, 40, working_hours(), [], [], bookings)
    assert at("14:20") in slots  # исходный сценарий бага

    rows = _slot_rows(DATE, TZ, START, END, free)
    free_starts = {row[0] for row in rows if row[2]}
    assert all(slot in free_starts for slot in slots)


def test_rows_stay_half_hour_when_bookings_are_on_grid():
    """Если границы записей лежат на сетке, разбивка остаётся прежней: ровные получасовые строки."""
    rows = _slot_rows(DATE, TZ, START, END, free_for([booking("12:30", "13:30")]))

    assert len(rows) == 20
    assert {row[1] - row[0] for row in rows} == {dt.timedelta(minutes=30)}
    assert [label(r)[2] for r in rows if label(r)[0] in ("12:30", "13:00")] == [False, False]


def test_rows_cover_working_day_without_gaps():
    rows = _slot_rows(DATE, TZ, START, END, free_for([booking("12:30", "14:20"), booking("17:10", "18:00")]))

    assert rows[0][0] == at("10:00")
    assert rows[-1][1] == at("20:00")
    assert all(prev[1] == nxt[0] for prev, nxt in zip(rows, rows[1:]))


def test_consecutive_busy_rows_are_merged_into_one():
    """Запись 10:30–12:30 — одна строка «Занято», а не четыре по получаса."""
    rows = _merge_rows(_slot_rows(DATE, TZ, START, END, free_for([booking("10:30", "12:30")])))
    labels = [label(r) for r in rows]

    assert ("10:30", "12:30", False) in labels
    assert sum(1 for _, _, free in labels if not free) == 1
    assert all(prev[1] == nxt[0] for prev, nxt in zip(rows, rows[1:]))


def test_free_window_between_bookings_is_one_row():
    """Окно между записями 13:30–14:50 и 15:30–19:00: 14:50–15:30 — одна строка, а не 14:50–15:00 + 15:00–15:30."""
    bookings = [booking("13:30", "14:50"), booking("15:30", "19:00")]
    rows = _merge_rows(_slot_rows(DATE, TZ, START, END, free_for(bookings)))
    labels = [label(r) for r in rows]

    assert labels == [
        ("10:00", "13:30", True),
        ("13:30", "14:50", False),
        ("14:50", "15:30", True),
        ("15:30", "19:00", False),
        ("19:00", "20:00", True),
    ]


@pytest.mark.skipif(
    not (os.path.exists(schedule_image._FONT_BOLD) and os.path.exists(schedule_image._FONT_REGULAR)),
    reason="Шрифты DejaVu (Linux-пути из schedule_image.py) не найдены — например, при запуске на Windows",
)
def test_render_day_schedule_image_returns_png():
    png = render_day_schedule_image(DATE, TZ, START, END, free_for([booking("12:30", "14:20")]))

    assert png.startswith(b"\x89PNG")
