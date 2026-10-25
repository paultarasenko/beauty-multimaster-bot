import calendar
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BlockedPeriod, Booking, BookingStatus, BreakPeriod, WorkingDayOverride, WorkingHours
from app.database.repositories.schedule_repository import ScheduleRepository
from app.services.availability_service import AvailabilityService, Interval

PERIOD_TODAY = "today"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIOD_3MONTHS = "3months"
PERIOD_6MONTHS = "6months"


@dataclass(frozen=True)
class PeriodStats:
    bookings_count: int  # все записи периода: и уже прошедшие, и ещё предстоящие
    actual_revenue: Decimal  # заработано по факту: услуги периода, которые уже закончились
    expected_revenue: Decimal  # заработано + то, что ещё впереди (идёт сейчас или предстоит) — итог периода
    free_minutes: int  # рабочее время от «сейчас» до конца периода, не занятое ничем


def get_period_dates(period: str, today: dt.date) -> tuple[dt.date, dt.date]:
    """Первый и последний день периода (включительно).

    Неделя — календарная (понедельник–воскресенье). Месяц — с первого по последнее число
    текущего календарного месяца (включая уже подтверждённые записи на будущие дни месяца).
    """
    if period == PERIOD_TODAY:
        return today, today
    if period == PERIOD_WEEK:
        start = today - dt.timedelta(days=today.weekday())
        return start, start + dt.timedelta(days=6)
    if period == PERIOD_MONTH:
        last_day = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=1), today.replace(day=last_day)
    if period == PERIOD_3MONTHS:
        return today, today + dt.timedelta(days=89)  # 90 дней включительно, как «На три месяца» в Записях
    if period == PERIOD_6MONTHS:
        return today, today + dt.timedelta(days=179)  # 180 дней включительно, как горизонт записи вперёд
    raise ValueError(f"Неизвестный период: {period}")


def get_free_hours_end_date(period: str, today: dt.date, period_end: dt.date) -> dt.date:
    """Последний день, до которого считаются свободные часы. Для «месяца» — конец календарного
    месяца (сколько свободного времени осталось), для остальных периодов — конец самого периода."""
    if period == PERIOD_MONTH:
        return today.replace(day=calendar.monthrange(today.year, today.month)[1])
    return period_end


def calculate_period_stats(
    *,
    bookings: list[Booking],
    start_date: dt.date,
    end_date: dt.date,
    tz: dt.tzinfo,
    now: dt.datetime,
    working_hours_by_weekday: dict[int, WorkingHours | None],
    breaks_by_weekday: dict[int, list[BreakPeriod]],
    blocked_periods: list[BlockedPeriod],
    availability: AvailabilityService | None = None,
    free_end_date: dt.date | None = None,
    working_hours_overrides: dict[dt.date, WorkingDayOverride] | None = None,
) -> PeriodStats:
    """Чистая функция расчёта: ничего не читает из БД, получает уже загруженные данные.

    bookings — подтверждённые и выполненные записи, которые пересекаются с периодом
    (и с интервалом расчёта свободных часов, если он длиннее периода).
    Отменённые и «не пришёл» сюда попадать не должны.
    free_end_date — последний день расчёта свободных часов (по умолчанию — end_date).
    working_hours_overrides — исключения на даты (дата -> исключение): главнее часов по дню недели.
    """
    availability = availability or AvailabilityService()
    period_start = dt.datetime.combine(start_date, dt.time.min, tzinfo=tz)
    period_end = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

    count = 0
    actual = Decimal("0")
    expected = Decimal("0")
    busy: list[Interval] = []

    for booking in bookings:
        if booking.status == BookingStatus.CONFIRMED.value:
            busy.append(Interval(booking.start_datetime, booking.end_datetime))

        # В статистику периода попадают записи, которые в нём начинаются.
        if not (period_start <= booking.start_datetime < period_end):
            continue
        count += 1
        price = Decimal(str(booking.service_price_snapshot))
        expected += price  # ожидаемый доход включает и уже заработанное, и предстоящее
        if booking.status == BookingStatus.COMPLETED.value or booking.end_datetime <= now:
            actual += price

    overrides = working_hours_overrides or {}
    free = dt.timedelta()
    day = start_date
    while day <= (free_end_date or end_date):
        weekday = day.weekday()
        intervals = availability.get_free_intervals(
            day,
            tz,
            overrides[day] if day in overrides else working_hours_by_weekday.get(weekday),
            breaks_by_weekday.get(weekday, []),
            blocked_periods,
            busy,
        )
        for interval in intervals:
            # Прошедшее время свободным не считаем — только от «сейчас» и дальше.
            begin = max(interval.start, now)
            if interval.end > begin:
                free += interval.end - begin
        day += dt.timedelta(days=1)

    return PeriodStats(
        bookings_count=count,
        actual_revenue=actual,
        expected_revenue=expected,
        free_minutes=int(free.total_seconds() // 60),
    )


async def get_period_stats(
    session: AsyncSession, master_id: int, tz: dt.tzinfo, period: str, now: dt.datetime
) -> tuple[PeriodStats, dt.date, dt.date]:
    """Загружает данные из БД и считает статистику. Возвращает статистику и границы периода."""
    start_date, end_date = get_period_dates(period, now.date())
    period_start = dt.datetime.combine(start_date, dt.time.min, tzinfo=tz)
    period_end = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

    # Свободные часы могут считаться дальше конца периода (для «месяца» — до конца месяца),
    # поэтому записи и блокировки грузим до этой границы.
    free_end_date = get_free_hours_end_date(period, now.date(), end_date)
    load_end = dt.datetime.combine(free_end_date + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

    result = await session.execute(
        select(Booking).where(
            Booking.master_id == master_id,
            Booking.status.in_([BookingStatus.CONFIRMED.value, BookingStatus.COMPLETED.value]),
            Booking.start_datetime < load_end,
            Booking.end_datetime > period_start,
        )
    )
    bookings = list(result.scalars().all())

    schedule_repo = ScheduleRepository(session, master_id)
    working_hours_by_weekday: dict[int, WorkingHours | None] = {}
    breaks_by_weekday: dict[int, list[BreakPeriod]] = {}
    for weekday in range(7):
        working_hours_by_weekday[weekday] = await schedule_repo.get_working_hours(weekday)
        breaks_by_weekday[weekday] = await schedule_repo.get_breaks(weekday)
    blocked_periods = await schedule_repo.get_blocked_periods(period_start, load_end)
    working_hours_overrides = await schedule_repo.get_overrides_in_range(start_date, free_end_date)

    stats = calculate_period_stats(
        bookings=bookings,
        start_date=start_date,
        end_date=end_date,
        tz=tz,
        now=now,
        working_hours_by_weekday=working_hours_by_weekday,
        breaks_by_weekday=breaks_by_weekday,
        blocked_periods=blocked_periods,
        free_end_date=free_end_date,
        working_hours_overrides=working_hours_overrides,
    )
    return stats, start_date, end_date