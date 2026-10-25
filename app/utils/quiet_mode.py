"""Тихий режим мастера: чистые функции (без БД и aiogram) — попадание времени в окно и проверка окна."""

import datetime as dt
from dataclasses import dataclass

DEFAULT_QUIET_START = dt.time(22, 0)
DEFAULT_QUIET_END = dt.time(8, 0)


class QuietModeError(ValueError):
    """Недопустимое значение; текст безопасен для показа мастеру."""


@dataclass(frozen=True)
class QuietModeData:
    """Снимок тихого режима мастера. По умолчанию (строки в БД нет) режим выключен."""

    enabled: bool = False
    start_time: dt.time = DEFAULT_QUIET_START
    end_time: dt.time = DEFAULT_QUIET_END


def format_time(value: dt.time) -> str:
    return value.strftime("%H:%M")


def format_window(start: dt.time, end: dt.time) -> str:
    return f"{format_time(start)}–{format_time(end)}"


def is_quiet_time(start: dt.time, end: dt.time, local_time: dt.time) -> bool:
    """Попадает ли настенное время в окно [start, end); окно может переходить через полночь (22:00–08:00)."""
    if start == end:
        return False
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def validate_window(start: dt.time, end: dt.time) -> None:
    if start == end:
        raise QuietModeError("Начало и конец тихого режима не должны совпадать.")
