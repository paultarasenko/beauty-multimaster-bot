"""Общая часть календаря месяца для «Проверить время» (клиент) и «Закрыть время» (мастер): состояние дней и разметка."""

import datetime as dt

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.callback_data import CalendarCallback
from app.bot.keyboards.client_keyboards import (
    CALENDAR_BUSY,
    CALENDAR_FILL,
    CALENDAR_FREE,
    CALENDAR_OFF,
    calendar_keyboard,
)
from app.database.models import WorkingDayOverride, WorkingHours
from app.database.repositories.schedule_repository import ScheduleRepository
from app.services.booking_service import BookingService, booking_service_for
from app.tenancy import TenantContext

BOOKING_HORIZON_DAYS = 180  # на сколько дней вперёд (≈ 6 месяцев) можно записаться; общая граница всех календарей

CALENDAR_NOTICES = {
    CALENDAR_BUSY: "На этот день нет свободного времени",
    CALENDAR_OFF: "Этот день выходной",
}


# Блок кнопок не шире текста сообщения: строка из невидимых символов растягивает сообщение на максимальную ширину,
# и календарь выглядит одинаково при любом заголовке (с названием услуги, «новой датой» и т.д.).
CALENDAR_TEXT_PADDING = CALENDAR_FILL * 60


def calendar_text(header: str) -> str:
    """Текст сообщения с календарём: заголовок и строка-распорка, фиксирующая ширину блока."""
    return f"{header}\n{CALENDAR_TEXT_PADDING}"


def _is_working_day(working_hours: WorkingHours | WorkingDayOverride | None) -> bool:
    return (
        working_hours is not None
        and working_hours.is_working
        and working_hours.start_time is not None
        and working_hours.end_time is not None
    )


async def has_working_days(session: AsyncSession, tenant: TenantContext, tz: dt.tzinfo) -> bool:
    """Есть ли у мастера хоть один рабочий день: по расписанию недели или открытая на дату в пределах окна записи."""
    schedule_repo = ScheduleRepository(session, tenant.master_id)
    working_hours = await schedule_repo.get_all_working_hours()
    if any(_is_working_day(item) for item in working_hours.values()):
        return True
    today = dt.datetime.now(tz).date()
    overrides = await schedule_repo.get_overrides_in_range(today, today + dt.timedelta(days=BOOKING_HORIZON_DAYS - 1))
    return any(_is_working_day(item) for item in overrides.values())


async def _day_states(
    schedule_repo: ScheduleRepository,
    booking_service: BookingService,
    now: dt.datetime,
    month_first: dt.date,
    last_date: dt.date,
    duration: int | None = None,
) -> dict[dt.date, str]:
    """Состояние каждого дня месяца от сегодня до границы записи: свободен / занят / выходной.

    «Занят» — день, закрытый мастером особо, или рабочий день, где не помещается услуга длиной `duration` минут (для сегодняшнего дня — после текущего
    момента; учитываются записи, перерывы и блокировки). Без `duration` берётся самая короткая активная услуга,
    а если активных услуг нет — достаточно любого свободного времени.
    """
    today = now.date()
    next_month = (month_first + dt.timedelta(days=31)).replace(day=1)
    first = max(month_first, today)
    last = min(next_month - dt.timedelta(days=1), last_date)
    days = [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]

    working_hours = await schedule_repo.get_all_working_hours()
    overrides = await schedule_repo.get_overrides_in_range(first, last)  # исключение на дату главнее дня недели

    def hours_of(day: dt.date) -> WorkingHours | WorkingDayOverride | None:
        return overrides[day] if day in overrides else working_hours.get(day.weekday())

    # Выходной по расписанию — «➖»; день, закрытый мастером особо (исключение на дату), выглядит как занятый — «🔴».
    states = {
        day: CALENDAR_BUSY if day in overrides else CALENDAR_OFF for day in days if not _is_working_day(hours_of(day))
    }
    working_days = [day for day in days if day not in states]

    if duration is None:
        active_services = await booking_service.services.get_active()
        duration = min((service.occupied_minutes for service in active_services), default=1)
    # dates_with_slots применяет `now` только к первой дате списка — передаём его, лишь если это сегодня.
    free_days = set(
        await booking_service.dates_with_slots(
            working_days, duration, now if working_days and working_days[0] == today else None
        )
    )
    states.update({day: CALENDAR_FREE if day in free_days else CALENDAR_BUSY for day in working_days})
    return states


def booking_window(now: dt.datetime) -> tuple[dt.date, dt.date]:
    """Первый и последний день, на которые можно записаться: от сегодня на BOOKING_HORIZON_DAYS дней."""
    today = now.date()
    return today, today + dt.timedelta(days=BOOKING_HORIZON_DAYS - 1)


def clamp_month(requested_month: dt.date, today: dt.date, last_date: dt.date) -> dt.date:
    """Первое число месяца, не выходящее за окно записи (запрошенный месяц вне окна заменяется ближайшим)."""
    return min(max(requested_month.replace(day=1), today.replace(day=1)), last_date.replace(day=1))


async def calendar_markup(
    session: AsyncSession,
    tenant: TenantContext,
    tz: dt.tzinfo,
    requested_month: dt.date,
    *,
    duration: int | None = None,
    with_back: bool = False,
) -> InlineKeyboardMarkup:
    """Календарь запрошенного месяца; месяц за пределами окна записи заменяется ближайшим допустимым.

    `duration` — длина услуги в минутах, под которую ищется свободное время (без неё — самая короткая услуга);
    `with_back` — добавить «⬅️ Назад» рядом с «Отменой».
    """
    now = dt.datetime.now(tz)
    today, last_date = booking_window(now)
    month_first = clamp_month(requested_month, today, last_date)
    states = await _day_states(
        ScheduleRepository(session, tenant.master_id),
        await booking_service_for(session, tz, tenant),
        now,
        month_first,
        last_date,
        duration,
    )
    return calendar_keyboard(
        month_first.year, month_first.month, today=today, last_date=last_date, states=states, with_back=with_back
    )


async def has_free_dates(session: AsyncSession, tenant: TenantContext, tz: dt.tzinfo, duration: int) -> bool:
    """Есть ли хоть одна дата в окне записи, где помещается услуга длиной `duration` минут (одна пакетная проверка)."""
    now = dt.datetime.now(tz)
    today, _ = booking_window(now)
    days = [today + dt.timedelta(days=offset) for offset in range(BOOKING_HORIZON_DAYS)]
    booking_service = await booking_service_for(session, tz, tenant)
    return bool(await booking_service.dates_with_slots(days, duration, now))


async def edit_calendar(callback: CallbackQuery, header: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(calendar_text(header), reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
    await callback.answer()


async def calendar_notice(callback: CallbackQuery, callback_data: CalendarCallback) -> None:
    """Занятый день и выходной отвечают всплывающим сообщением; остальные неактивные ячейки — молча.

    Общий обработчик: каждый сценарий с календарём регистрирует его на своё состояние выбора даты."""
    notice = CALENDAR_NOTICES.get(callback_data.action)
    await callback.answer(notice, show_alert=notice is not None)
