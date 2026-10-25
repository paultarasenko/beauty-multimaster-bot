import datetime as dt
from dataclasses import dataclass

from app.database.models import BlockedPeriod, BreakPeriod, WorkingHours


@dataclass(frozen=True)
class Interval:
    start: dt.datetime
    end: dt.datetime


def _subtract_interval(free: list[Interval], busy: Interval) -> list[Interval]:
    """Вычитает один занятый интервал из списка свободных, разбивая их при необходимости."""
    result: list[Interval] = []
    for interval in free:
        # Нет пересечения — интервал остаётся как есть.
        if busy.end <= interval.start or busy.start >= interval.end:
            result.append(interval)
            continue
        # Пересечение слева от busy остаётся свободным.
        if interval.start < busy.start:
            result.append(Interval(interval.start, busy.start))
        # Пересечение справа от busy остаётся свободным.
        if interval.end > busy.end:
            result.append(Interval(busy.end, interval.end))
    return result


class AvailabilityService:
    """Чистый, без сайд-эффектов сервис расчёта свободных слотов.

    Не обращается к БД сам — получает уже загруженные данные, поэтому легко тестируется
    без реальной базы (хотя итоговая защита от гонки всё равно на уровне PostgreSQL exclusion constraint).
    """

    def __init__(self, slot_interval_minutes: int = 30):
        self.slot_interval_minutes = slot_interval_minutes

    def get_free_intervals(
        self,
        date: dt.date,
        tz: dt.tzinfo,
        working_hours: WorkingHours | None,
        breaks: list[BreakPeriod],
        blocked_periods: list[BlockedPeriod],
        confirmed_bookings_ranges: list[Interval],
    ) -> list[Interval]:
        if working_hours is None or not working_hours.is_working:
            return []
        if working_hours.start_time is None or working_hours.end_time is None:
            return []

        day_start = dt.datetime.combine(date, working_hours.start_time, tzinfo=tz)
        day_end = dt.datetime.combine(date, working_hours.end_time, tzinfo=tz)
        free = [Interval(day_start, day_end)]

        for br in breaks:
            busy = Interval(
                dt.datetime.combine(date, br.start_time, tzinfo=tz),
                dt.datetime.combine(date, br.end_time, tzinfo=tz),
            )
            free = _subtract_interval(free, busy)

        for bp in blocked_periods:
            free = _subtract_interval(free, Interval(bp.start_datetime, bp.end_datetime))

        for booking_range in confirmed_bookings_ranges:
            free = _subtract_interval(free, booking_range)

        return [iv for iv in free if iv.end > iv.start]

    def get_available_slots(
        self,
        date: dt.date,
        tz: dt.tzinfo,
        service_duration_minutes: int,
        working_hours: WorkingHours | None,
        breaks: list[BreakPeriod],
        blocked_periods: list[BlockedPeriod],
        confirmed_bookings_ranges: list[Interval],
        now: dt.datetime | None = None,
    ) -> list[dt.datetime]:
        """Возвращает список валидных времён начала слота — таких, что услуга полностью
        помещается в свободный интервал и не пересекает ни break, ни blocked period, ни booking.
        """
        free_intervals = self.get_free_intervals(
            date, tz, working_hours, breaks, blocked_periods, confirmed_bookings_ranges
        )
        if not free_intervals:
            return []
        duration = dt.timedelta(minutes=service_duration_minutes)
        step = dt.timedelta(minutes=self.slot_interval_minutes)

        # Слоты строим по ровной сетке от начала рабочего дня (10:00, 10:30, 11:00 ...) и в таймзоне мастера.
        # Дополнительно, если свободный интервал начинается сразу после записи, предлагаем и время её
        # окончания (услуга 40 минут с 10:00 -> следующий слот 10:40, а не 11:00). Границы интервалов
        # приходят из БД в UTC, поэтому такой слот приводим к таймзоне мастера.
        day_start = dt.datetime.combine(date, working_hours.start_time, tzinfo=tz)

        booking_ends = {booking_range.end for booking_range in confirmed_bookings_ranges}

        slots: list[dt.datetime] = []
        for interval in free_intervals:
            steps_to_skip = max(0, -((day_start - interval.start) // step))  # первый узел сетки не раньше начала интервала
            slot_start = day_start + steps_to_skip * step
            if interval.start in booking_ends and interval.start != slot_start:
                after_booking = interval.start.astimezone(tz)
                if after_booking + duration <= interval.end and (now is None or after_booking >= now):
                    slots.append(after_booking)
            while slot_start + duration <= interval.end:
                if now is None or slot_start >= now:
                    slots.append(slot_start)
                slot_start += step
        return slots

    def has_any_slot(
        self,
        date: dt.date,
        tz: dt.tzinfo,
        service_duration_minutes: int,
        working_hours: WorkingHours | None,
        breaks: list[BreakPeriod],
        blocked_periods: list[BlockedPeriod],
        confirmed_bookings_ranges: list[Interval],
        now: dt.datetime | None = None,
    ) -> bool:
        return (
            len(
                self.get_available_slots(
                    date,
                    tz,
                    service_duration_minutes,
                    working_hours,
                    breaks,
                    blocked_periods,
                    confirmed_bookings_ranges,
                    now,
                )
            )
            > 0
        )