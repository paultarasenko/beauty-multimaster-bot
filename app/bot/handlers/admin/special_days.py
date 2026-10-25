"""Особые дни мастера: исключения из расписания по дням недели на конкретную дату.

Мастер может открыть одну дату, когда по расписанию дня недели не работает (например, одну субботу), изменить часы
одного дня или закрыть одну дату целиком (отпуск, праздник). Исключение хранится в working_day_overrides и главнее
расписания по дням недели; «Вернуть по расписанию» его удаляет.
"""

import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.calendar_view import booking_window, clamp_month
from app.bot.handlers.admin.schedule import WEEKDAY_NAMES_RU, WEEKDAY_SHORT_RU, _day_time_grid
from app.bot.keyboards.admin_keyboards import special_day_menu_keyboard
from app.bot.keyboards.callback_data import (
    AdminScheduleCallback,
    CalendarCallback,
    ConfirmCallback,
    DateCallback,
    NavCallback,
    TimeCallback,
)
from app.bot.keyboards.client_keyboards import CALENDAR_BUSY_MARK, calendar_pick_keyboard, confirm_keyboard, times_keyboard
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminSpecialDayFSM
from app.database.models import WorkingDayOverride, WorkingHours
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.schedule_repository import ScheduleRepository
from app.utils.timezone import get_master_tz, to_local
from app.tenancy import TenantContext

router = Router(name="admin_special_days")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

MARK_OPEN = "✅"
MARK_CLOSED = CALENDAR_BUSY_MARK  # закрытый день выглядит так же, как занятый
UPCOMING_LIMIT = 10
MAX_CONFLICTS_SHOWN = 15


def _hours_text(hours: WorkingHours | WorkingDayOverride | None) -> str:
    """«10:00 — 15:00» или «выходной»."""
    if hours is None or not hours.is_working or hours.start_time is None or hours.end_time is None:
        return "выходной"
    return f"{hours.start_time.strftime('%H:%M')} — {hours.end_time.strftime('%H:%M')}"


def _is_open(hours: WorkingHours | WorkingDayOverride | None) -> bool:
    return hours is not None and hours.is_working and hours.start_time is not None and hours.end_time is not None


def _day_title(day: dt.date) -> str:
    return f"{WEEKDAY_NAMES_RU[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


async def _calendar_screen(
    session: AsyncSession, tenant: TenantContext, tz: dt.tzinfo, requested_month: dt.date
) -> tuple[str, InlineKeyboardMarkup]:
    now = dt.datetime.now(tz)
    today, last_date = booking_window(now)
    month_first = clamp_month(requested_month, today, last_date)
    month_last = (month_first + dt.timedelta(days=31)).replace(day=1) - dt.timedelta(days=1)

    repo = ScheduleRepository(session, tenant.master_id)
    marks = {
        day: MARK_OPEN if override.is_working else MARK_CLOSED
        for day, override in (await repo.get_overrides_in_range(month_first, month_last)).items()
    }
    upcoming = await repo.get_upcoming_overrides(today, UPCOMING_LIMIT)
    lines = [
        "📌 Изменить график конкретного дня",
        "Выберите дату, чтобы открыть выходной, изменить часы или закрыть рабочий день.",
        f"{MARK_OPEN} — открыт особо, {MARK_CLOSED} — закрыт особо.",
        "",
    ]
    if upcoming:
        lines.append("Ближайшие изменения графика:")
        lines += [
            f"{MARK_OPEN if item.is_working else MARK_CLOSED} {WEEKDAY_SHORT_RU[item.day.weekday()]} "
            f"{item.day.strftime('%d.%m')}: {_hours_text(item)}"
            for item in upcoming
        ]
    else:
        lines.append("Изменений графика пока нет.")
    markup = calendar_pick_keyboard(month_first.year, month_first.month, today=today, last_date=last_date, marks=marks)
    return "\n".join(lines), markup


async def _show_calendar(
    callback: CallbackQuery, session: AsyncSession, tenant: TenantContext, tz: dt.tzinfo, requested_month: dt.date
) -> None:
    text, markup = await _calendar_screen(session, tenant, tz, requested_month)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
    await callback.answer()


async def _day_menu_screen(
    session: AsyncSession, tenant: TenantContext, day: dt.date
) -> tuple[str, InlineKeyboardMarkup]:
    repo = ScheduleRepository(session, tenant.master_id)
    weekly = await repo.get_working_hours(day.weekday())
    override = await repo.get_override(day)
    effective = override if override is not None else weekly
    lines = [f"📌 {_day_title(day)}", f"По расписанию: {_hours_text(weekly)}"]
    if override is not None:
        lines.append(f"Изменено на этот день: {_hours_text(override)}")
    lines.append(f"Сейчас: {_hours_text(effective)}")
    return "\n".join(lines), special_day_menu_keyboard(_is_open(effective), override is not None)


async def _show_day_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    day = dt.date.fromisoformat((await state.get_data())["special_day"])
    text, markup = await _day_menu_screen(session, tenant, day)
    await state.set_state(AdminSpecialDayFSM.day_menu)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


async def _find_conflicts(session: AsyncSession, tenant: TenantContext, tz: dt.tzinfo, day: dt.date) -> list:
    """Подтверждённые записи, которые начинаются в этот день (по часам мастера)."""
    day_start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    day_end = dt.datetime.combine(day, dt.time.max, tzinfo=tz)
    return list(await BookingRepository(session, tenant.master_id).get_confirmed_in_range(day_start, day_end))


def _conflicts_text(conflicts: list, tz: dt.tzinfo, day: dt.date) -> str:
    shown = conflicts[:MAX_CONFLICTS_SHOWN]
    lines = "\n".join(
        f"• {to_local(b.start_datetime, tz).strftime('%H:%M')} — {escape(b.service_name_snapshot)}" for b in shown
    )
    more = f"\n…и ещё {len(conflicts) - len(shown)}" if len(conflicts) > len(shown) else ""
    return (
        f"⚠️ На {day.strftime('%d.%m.%Y')} уже есть подтверждённые записи:\n\n{lines}{more}\n\n"
        "Они не будут отменены автоматически и останутся как есть, если сделать день выходным. "
        "Клиентам об этом ничего не придёт — отменить или перенести их нужно будет вручную в разделе «Записи».\n\n"
        "Всё равно сделать этот день выходным?"
    )


# --- Календарь выбора даты ---


@router.callback_query(F.data == "sd:open")
async def open_special_days(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    await state.clear()
    await state.set_state(AdminSpecialDayFSM.select_date)
    await _show_calendar(callback, session, tenant, tz, dt.datetime.now(tz).date())


@router.callback_query(AdminSpecialDayFSM.select_date, CalendarCallback.filter(F.action == "month"))
async def change_month(
    callback: CallbackQuery, callback_data: CalendarCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    try:
        requested_month = dt.date(callback_data.year, callback_data.month, 1)
    except ValueError:
        await callback.answer()
        return
    tz = await get_master_tz(session, tenant.master_id)
    await _show_calendar(callback, session, tenant, tz, requested_month)


@router.callback_query(AdminSpecialDayFSM.select_date, CalendarCallback.filter(F.action == "noop"))
async def calendar_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(AdminSpecialDayFSM.select_date, CalendarCallback.filter(F.action == "cancel"))
async def cancel_calendar(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@router.callback_query(AdminSpecialDayFSM.select_date, DateCallback.filter())
async def select_day(
    callback: CallbackQuery, callback_data: DateCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    today, last_date = booking_window(dt.datetime.now(tz))
    try:
        day = dt.date.fromisoformat(callback_data.date)
    except ValueError:
        day = None
    if day is None or not today <= day <= last_date:
        await callback.answer("Эта дата недоступна. Выберите другую.", show_alert=True)
        return
    await state.update_data(special_day=day.isoformat())
    await _show_day_menu(callback, state, session, tenant)


@router.callback_query(AdminSpecialDayFSM.day_menu, NavCallback.filter(F.target == "back"))
async def back_to_calendar(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    day = dt.date.fromisoformat((await state.get_data())["special_day"])
    await state.set_state(AdminSpecialDayFSM.select_date)
    await _show_calendar(callback, session, tenant, tz, day)


# --- Открыть день / изменить часы ---


async def _show_start_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    day = dt.date.fromisoformat((await state.get_data())["special_day"])
    grid = _day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes)
    await state.set_state(AdminSpecialDayFSM.select_start_time)
    await callback.message.edit_text(
        f"📌 {_day_title(day)}\n\n🕐 Выберите время начала работы:", reply_markup=times_keyboard(grid, with_main_menu=False)
    )
    await callback.answer()


@router.callback_query(AdminSpecialDayFSM.day_menu, AdminScheduleCallback.filter(F.action == "sd_open"))
async def start_open(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_start_time(callback, state, session, tenant)


@router.callback_query(AdminSpecialDayFSM.select_start_time, NavCallback.filter(F.target == "back"))
async def back_to_day_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_day_menu(callback, state, session, tenant)


@router.callback_query(AdminSpecialDayFSM.select_start_time, TimeCallback.filter())
async def select_start_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    start = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)
    options = [t for t in _day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes) if t.time() > start.time()]
    if not options:
        await callback.answer("Для этого времени начала нет доступного времени окончания.", show_alert=True)
        return
    day = dt.date.fromisoformat((await state.get_data())["special_day"])
    await state.update_data(start_time=start.time().isoformat())
    await state.set_state(AdminSpecialDayFSM.select_end_time)
    await callback.message.edit_text(
        f"📌 {_day_title(day)}\n🕐 Начало: {start.strftime('%H:%M')}\n\n🕐 Выберите время окончания работы:",
        reply_markup=times_keyboard(options, with_main_menu=False),
    )
    await callback.answer()


@router.callback_query(AdminSpecialDayFSM.select_end_time, NavCallback.filter(F.target == "back"))
async def back_to_start_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_start_time(callback, state, session, tenant)


@router.callback_query(AdminSpecialDayFSM.select_end_time, TimeCallback.filter())
async def select_end_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    end = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)
    data = await state.get_data()
    day = dt.date.fromisoformat(data["special_day"])
    start_time = dt.time.fromisoformat(data["start_time"])
    if end.time() <= start_time:  # устаревшая или подделанная кнопка: конец не позже начала в БД не пройдёт
        await callback.answer("Время окончания должно быть позже начала.", show_alert=True)
        return
    await state.update_data(end_time=end.time().isoformat())
    await state.set_state(AdminSpecialDayFSM.confirm)
    await callback.message.edit_text(
        f"Сохранить изменение графика?\n\n{_day_title(day)}: {start_time.strftime('%H:%M')} — {end.strftime('%H:%M')}",
        reply_markup=confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(AdminSpecialDayFSM.confirm, ConfirmCallback.filter(F.action == "edit"))
async def edit_hours(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_start_time(callback, state, session, tenant)


@router.callback_query(AdminSpecialDayFSM.confirm, ConfirmCallback.filter(F.action == "yes"))
async def confirm_open(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    data = await state.get_data()
    day = dt.date.fromisoformat(data["special_day"])
    start_time = dt.time.fromisoformat(data["start_time"])
    end_time = dt.time.fromisoformat(data["end_time"])
    await ScheduleRepository(session, tenant.master_id).set_override(day, True, start_time, end_time)
    await session.commit()
    await state.clear()
    await callback.message.edit_text(
        f"✅ {_day_title(day)}: {start_time.strftime('%H:%M')} — {end_time.strftime('%H:%M')}. "
        "Клиенты смогут записаться на этот день."
    )
    await callback.answer()


# --- Закрыть день / вернуть по расписанию ---


async def _close_day(session: AsyncSession, tenant: TenantContext, day: dt.date) -> None:
    await ScheduleRepository(session, tenant.master_id).set_override(day, False, None, None)
    await session.commit()


async def _reset_day(session: AsyncSession, tenant: TenantContext, day: dt.date) -> str:
    repo = ScheduleRepository(session, tenant.master_id)
    await repo.delete_override(day)
    await session.commit()
    return _hours_text(await repo.get_working_hours(day.weekday()))


async def _ask_conflicts_or_apply(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext, pending: str
) -> None:
    """Закрытие дня (или возврат к выходному по расписанию) при подтверждённых записях требует подтверждения."""
    tz = await get_master_tz(session, tenant.master_id)
    day = dt.date.fromisoformat((await state.get_data())["special_day"])
    conflicts = await _find_conflicts(session, tenant, tz, day)
    if conflicts:
        await state.update_data(pending=pending)
        await state.set_state(AdminSpecialDayFSM.confirm_close)
        await callback.message.edit_text(_conflicts_text(conflicts, tz, day), reply_markup=confirm_keyboard())
        await callback.answer()
        return
    await _apply_pending(callback, state, session, tenant, pending)


async def _apply_pending(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext, pending: str
) -> None:
    day = dt.date.fromisoformat((await state.get_data())["special_day"])
    if pending == "close":
        await _close_day(session, tenant, day)
        text = f"✅ {_day_title(day)}: день закрыт, записаться на него нельзя."
    else:
        weekly = await _reset_day(session, tenant, day)
        text = f"✅ {_day_title(day)}: снова по расписанию ({weekly})."
    await state.clear()
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(AdminSpecialDayFSM.day_menu, AdminScheduleCallback.filter(F.action == "sd_close"))
async def start_close(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _ask_conflicts_or_apply(callback, state, session, tenant, "close")


@router.callback_query(AdminSpecialDayFSM.day_menu, AdminScheduleCallback.filter(F.action == "sd_reset"))
async def start_reset(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    day = dt.date.fromisoformat((await state.get_data())["special_day"])
    weekly = await ScheduleRepository(session, tenant.master_id).get_working_hours(day.weekday())
    if _is_open(weekly):  # день и так рабочий по расписанию — записи под угрозой нет
        await _apply_pending(callback, state, session, tenant, "reset")
        return
    await _ask_conflicts_or_apply(callback, state, session, tenant, "reset")


@router.callback_query(AdminSpecialDayFSM.confirm_close, ConfirmCallback.filter(F.action == "edit"))
async def cancel_close(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_day_menu(callback, state, session, tenant)


@router.callback_query(AdminSpecialDayFSM.confirm_close, ConfirmCallback.filter(F.action == "yes"))
async def confirm_close(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    pending = (await state.get_data()).get("pending", "close")
    await _apply_pending(callback, state, session, tenant, pending)
