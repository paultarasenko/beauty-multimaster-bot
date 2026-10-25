"""Чистая (без БД и Telegram) логика напоминаний мастера: разбор ввода, расчёт срабатываний, подписи.

Всё считается по настенным часам таймзоны мастера, а в БД уходит UTC — поэтому напоминание
«в 19:30» остаётся в 19:30 и при переходе на летнее/зимнее время.
"""

import calendar
import datetime as dt
import re

from app.database.models.admin_reminder import Recurrence
from app.utils.timezone import MONTH_NAMES_RU, format_date_ru, to_local

MAX_YEARS_AHEAD = 120  # дата на столько лет вперёд и дальше считается ошибкой ввода

_SEPARATORS = re.compile(r"[\s.:/\-]")

_WEEKDAYS_DATIVE_PLURAL = [
    "понедельникам",
    "вторникам",
    "средам",
    "четвергам",
    "пятницам",
    "субботам",
    "воскресеньям",
]


# --- Разбор ввода мастера ---


def parse_date_input(raw: str) -> dt.date | None:
    """ДДММГГ, например «230986». Разделители (23.09.86) допускаются. Невозможная дата → None.

    Двузначный год трактуется по правилу Python/POSIX: 00–68 → 2000–2068, 69–99 → 1969–1999.
    """
    digits = _SEPARATORS.sub("", raw)
    if not re.fullmatch(r"\d{6}", digits):
        return None
    try:
        return dt.datetime.strptime(digits, "%d%m%y").date()
    except ValueError:
        return None


def parse_time_input(raw: str) -> dt.time | None:
    """ЧЧММ, например «1930». Разделители (19:30) допускаются. Невозможное время → None."""
    digits = _SEPARATORS.sub("", raw)
    if not re.fullmatch(r"\d{4}", digits):
        return None
    try:
        return dt.datetime.strptime(digits, "%H%M").time()
    except ValueError:
        return None


def is_date_too_far(day: dt.date, today: dt.date) -> bool:
    """Дата на MAX_YEARS_AHEAD лет вперёд (и дальше) от сегодняшней — явно опечатка."""
    limit_year = today.year + MAX_YEARS_AHEAD
    limit = dt.date(limit_year, today.month, min(today.day, calendar.monthrange(limit_year, today.month)[1]))
    return day >= limit


# --- Расчёт срабатываний ---


def _at(day: dt.date, time: dt.time, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.combine(day, time.replace(tzinfo=None), tzinfo=tz)


def _clamped(year: int, month: int, day: int) -> dt.date:
    """Число, которого нет в месяце (31 февраля, 29 февраля в невисокосный год), сдвигается на последний день месяца."""
    return dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_occurrence(
    recurrence: Recurrence | str,
    anchor_date: dt.date,
    remind_time: dt.time,
    tz: dt.tzinfo,
    after: dt.datetime,
) -> dt.datetime | None:
    """Первое срабатывание СТРОГО после `after` (tz-aware), в UTC. None — срабатываний больше нет.

    Дата начала (anchor_date) может быть в прошлом: повторяющееся напоминание тогда просто
    переходит на ближайшее будущее срабатывание. Пропущенные за время простоя бота не догоняются.
    """
    recurrence = Recurrence(recurrence)
    after_local = after.astimezone(tz)
    today = after_local.date()

    def result(candidate: dt.datetime) -> dt.datetime:
        return candidate.astimezone(dt.timezone.utc)

    if recurrence is Recurrence.ONCE:
        candidate = _at(anchor_date, remind_time, tz)
        return result(candidate) if candidate > after else None

    if recurrence is Recurrence.DAILY:
        day = max(anchor_date, today)
        for _ in range(2):
            candidate = _at(day, remind_time, tz)
            if candidate > after:
                return result(candidate)
            day += dt.timedelta(days=1)

    elif recurrence is Recurrence.WEEKLY:
        day = max(anchor_date, today)
        day += dt.timedelta(days=(anchor_date.weekday() - day.weekday()) % 7)
        for _ in range(2):
            candidate = _at(day, remind_time, tz)
            if candidate > after:
                return result(candidate)
            day += dt.timedelta(days=7)

    elif recurrence is Recurrence.MONTHLY:
        year, month = max((anchor_date.year, anchor_date.month), (today.year, today.month))
        for _ in range(3):
            day = _clamped(year, month, anchor_date.day)
            candidate = _at(day, remind_time, tz)
            if candidate > after and day >= anchor_date:
                return result(candidate)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    elif recurrence is Recurrence.YEARLY:
        year = max(anchor_date.year, today.year)
        for _ in range(3):
            day = _clamped(year, anchor_date.month, anchor_date.day)
            candidate = _at(day, remind_time, tz)
            if candidate > after and day >= anchor_date:
                return result(candidate)
            year += 1

    raise RuntimeError(f"Не удалось найти следующее срабатывание: {recurrence}, {anchor_date}, {remind_time}")


# --- Подписи для мастера ---


def recurrence_button_title(recurrence: Recurrence | str) -> str:
    return {
        Recurrence.ONCE: "Один раз",
        Recurrence.DAILY: "Каждый день",
        Recurrence.WEEKLY: "Раз в неделю",
        Recurrence.MONTHLY: "Раз в месяц",
        Recurrence.YEARLY: "Каждый год",
    }[Recurrence(recurrence)]


def describe_schedule(recurrence: Recurrence | str, anchor_date: dt.date, remind_time: dt.time) -> str:
    """Человекочитаемое расписание: «Раз в неделю: по понедельникам в 19:30»."""
    recurrence = Recurrence(recurrence)
    at = remind_time.strftime("%H:%M")

    if recurrence is Recurrence.ONCE:
        return f"Один раз: {format_date_ru(anchor_date)} {anchor_date.year}, {at}"
    if recurrence is Recurrence.DAILY:
        return f"Каждый день в {at}"
    if recurrence is Recurrence.WEEKLY:
        return f"Раз в неделю: по {_WEEKDAYS_DATIVE_PLURAL[anchor_date.weekday()]} в {at}"
    if recurrence is Recurrence.MONTHLY:
        note = " (в коротких месяцах — в последний день)" if anchor_date.day > 28 else ""
        return f"Раз в месяц: {anchor_date.day} числа в {at}{note}"
    return f"Каждый год: {anchor_date.day} {MONTH_NAMES_RU[anchor_date.month - 1]} в {at}"


def format_moment(value: dt.datetime, tz: dt.tzinfo) -> str:
    """Момент времени для мастера по его часам: «5 октября 2026, 19:30»."""
    local = to_local(value, tz)
    return f"{format_date_ru(local)} {local.year}, {local.strftime('%H:%M')}"
