import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import (
    schedule_lunch_confirm_keyboard,
    schedule_lunch_menu_keyboard,
    schedule_start_time_keyboard,
)
from app.bot.keyboards.callback_data import AdminScheduleCallback, ConfirmCallback, NavCallback, TimeCallback
from app.bot.keyboards.client_keyboards import confirm_keyboard, times_keyboard
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminScheduleFSM
from app.database.models import BreakPeriod, WorkingHours
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.schedule_repository import ScheduleRepository
from app.utils.timezone import get_master_tz, to_local
from app.tenancy import TenantContext

router = Router(name="admin_schedule")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

WEEKDAY_NAMES_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAY_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MAX_CONFLICTS_SHOWN = 15

DAY_START = dt.time(5, 0)
DAY_END = dt.time(23, 0)  # последний слот сетки при шаге slot_interval_minutes от DAY_START


def _day_icon(wh: WorkingHours | None) -> str:
    """✅ — рабочий день, 🚫 — выходной (те же условия, что в _format_hours)."""
    working = wh is not None and wh.is_working and wh.start_time is not None and wh.end_time is not None
    return "✅" if working else "🚫"


def _weekday_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *([InlineKeyboardButton(text=name, callback_data=f"wd:{i}")] for i, name in enumerate(WEEKDAY_NAMES_RU)),
            [InlineKeyboardButton(text="📌 Изменить график конкретного дня", callback_data="sd:open")],  # см. special_days.py
        ]
    )


async def _load_week_hours(session: AsyncSession, tenant: TenantContext) -> list[WorkingHours | None]:
    repo = ScheduleRepository(session, tenant.master_id)
    return [await repo.get_working_hours(i) for i in range(len(WEEKDAY_NAMES_RU))]


def _day_time_grid(tz: dt.tzinfo, step_minutes: int = 30) -> list[dt.datetime]:
    """Все времена с шагом slot_interval_minutes между DAY_START и DAY_END — для выбора начала/конца
    рабочего дня (в отличие от блокировки времени, тут ещё нет сохранённых рабочих часов, на которые
    можно опереться)."""
    anchor = dt.date.today()
    cur = dt.datetime.combine(anchor, DAY_START, tzinfo=tz)
    end = dt.datetime.combine(anchor, DAY_END, tzinfo=tz)
    step = dt.timedelta(minutes=step_minutes)
    grid: list[dt.datetime] = []
    while cur <= end:
        grid.append(cur)
        cur += step
    return grid


def _format_hours(wh: WorkingHours | None) -> str:
    """«10:00 — 19:00» или «выходной» — для списка недели и для экрана редактирования дня."""
    if wh is None or not wh.is_working or wh.start_time is None or wh.end_time is None:
        return "выходной"
    return f"{wh.start_time.strftime('%H:%M')} — {wh.end_time.strftime('%H:%M')}"


def _working_range(wh: WorkingHours | None) -> tuple[dt.time, dt.time] | None:
    """(начало, конец) рабочего дня или None, если день выходной (те же условия, что в _format_hours)."""
    if wh is None or not wh.is_working or wh.start_time is None or wh.end_time is None:
        return None
    return wh.start_time, wh.end_time


def _format_breaks(breaks: list[BreakPeriod]) -> str:
    """«13:00 — 14:00» (несколько перерывов через запятую) или пустая строка, если перерывов нет."""
    return ", ".join(f"{b.start_time.strftime('%H:%M')} — {b.end_time.strftime('%H:%M')}" for b in breaks)


async def _weekday_prompt(session: AsyncSession, weekday: int, tenant: TenantContext) -> str:
    """Экран выбора времени начала. Под названием дня — сохранённые сейчас часы работы: мастер видит,
    что именно меняет. Берём их из БД, а не из незавершённого ввода, поэтому после «Назад»/«Изменить» тоже актуальны."""
    repo = ScheduleRepository(session, tenant.master_id)
    current = _format_hours(await repo.get_working_hours(weekday))
    lunch = _format_breaks(await repo.get_breaks(weekday))
    lunch_line = f"🍽 Обед: {lunch}\n" if lunch else ""
    return (
        f"{WEEKDAY_NAMES_RU[weekday]}.\n"
        f"🕐 Сейчас: {current}\n"
        f"{lunch_line}\n"
        "Выберите время начала работы, или сделайте день выходным"
    )


async def _lunch_screen(session: AsyncSession, weekday: int, tenant: TenantContext) -> tuple[str, InlineKeyboardMarkup]:
    """Экран обеда выбранного дня: часы работы, текущий обед и кнопки действий."""
    repo = ScheduleRepository(session, tenant.master_id)
    hours = _format_hours(await repo.get_working_hours(weekday))
    breaks = await repo.get_breaks(weekday)
    text = f"{WEEKDAY_NAMES_RU[weekday]}\n🕐 Работа: {hours}\n🍽 Обед: {_format_breaks(breaks) or 'не задан'}"
    return text, schedule_lunch_menu_keyboard(bool(breaks))


def _lunch_start_options(grid: list[dt.datetime], work: tuple[dt.time, dt.time]) -> list[dt.datetime]:
    """Времена начала обеда: внутри рабочего дня и так, чтобы после них нашлось время окончания."""
    return [
        t for t in grid
        if work[0] <= t.time() < work[1] and _lunch_end_options(grid, t.time(), work)
    ]


def _lunch_end_options(grid: list[dt.datetime], start: dt.time, work: tuple[dt.time, dt.time]) -> list[dt.datetime]:
    """Времена окончания обеда: позже начала, но не позже конца рабочего дня."""
    return [t for t in grid if start < t.time() <= work[1]]


async def _find_lunch_conflicts(
    session: AsyncSession, tz: dt.tzinfo, weekdays: set[int], start: dt.time, end: dt.time, tenant: TenantContext
) -> list:
    """Будущие подтверждённые записи в эти дни недели, которые пересекаются с обедом start–end.

    Записи при сохранении обеда не отменяются и не переносятся — мастер только узнаёт о них."""
    upcoming = await BookingRepository(session, tenant.master_id).get_confirmed_upcoming(dt.datetime.now(tz))
    found = []
    for b in upcoming:
        b_start, b_end = to_local(b.start_datetime, tz), to_local(b.end_datetime, tz)
        if b_start.weekday() not in weekdays or b_start.time() >= end:
            continue
        if b_end.date() > b_start.date() or b_end.time() > start:
            found.append(b)
    return found


def _conflicts_text(conflicts: list, tz: dt.tzinfo) -> str:
    shown = conflicts[:MAX_CONFLICTS_SHOWN]
    lines = "\n".join(
        f"• {to_local(b.start_datetime, tz).strftime('%d.%m %H:%M')} — {escape(b.service_name_snapshot)}" for b in shown
    )
    more = f"\n…и ещё {len(conflicts) - len(shown)}" if len(conflicts) > len(shown) else ""
    return (
        "\n\n⚠️ На это время уже есть подтверждённые записи:\n"
        f"{lines}{more}\n\n"
        "Они остаются как есть: клиентам ничего не придёт. Отмените или перенесите их вручную в разделе «Записи»."
    )


async def _find_day_off_conflicts(session: AsyncSession, tz: dt.tzinfo, weekday: int, tenant: TenantContext) -> list:
    """Подтверждённые записи на будущее, которые приходятся на этот день недели.

    День недели не привязан к одной дате, поэтому смотрим вообще все будущие подтверждённые
    записи и оставляем те, что попадают на нужный weekday в таймзоне мастера.
    """
    now = dt.datetime.now(tz)
    upcoming = await BookingRepository(session, tenant.master_id).get_confirmed_upcoming(now)
    return [b for b in upcoming if to_local(b.start_datetime, tz).weekday() == weekday]


@router.message(F.text == "🕐 Расписание")
async def show_schedule(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await state.clear()  # если мастер не закончил редактирование другого дня — сбрасываем незавершённый ввод
    week_hours = await _load_week_hours(session, tenant=tenant)
    breaks_by_day = await ScheduleRepository(session, tenant.master_id).get_all_breaks()
    lines = ["🕐 Текущее расписание:\n"]
    for weekday, (name, wh) in enumerate(zip(WEEKDAY_NAMES_RU, week_hours)):
        lunch = _format_breaks(breaks_by_day.get(weekday, [])) if _working_range(wh) else ""
        lines.append(f"{_day_icon(wh)} {name}: {_format_hours(wh)}" + (f" · 🍽 {lunch}" if lunch else ""))
    lines.append("\nЧтобы изменить день, выберите его")
    await message.answer("\n".join(lines), reply_markup=_weekday_keyboard())


@router.callback_query(F.data.startswith("wd:"))
async def select_weekday(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    try:
        weekday = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        weekday = -1
    if not 0 <= weekday < len(WEEKDAY_NAMES_RU):  # подделанная кнопка: «wd:99» не должна дойти до БД
        await callback.answer("Эта кнопка устарела. Откройте раздел заново.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    await state.update_data(weekday=weekday)
    await state.set_state(AdminScheduleFSM.select_start_time)
    await callback.message.edit_text(await _weekday_prompt(session, weekday, tenant=tenant), reply_markup=schedule_start_time_keyboard(_day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes)))
    await callback.answer()


@router.callback_query(AdminScheduleFSM.select_start_time, NavCallback.filter(F.target == "back"))
async def back_to_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("🕐 Выберите день, который нужно изменить:", reply_markup=_weekday_keyboard())
    await callback.answer()


@router.callback_query(AdminScheduleFSM.select_start_time, AdminScheduleCallback.filter(F.action == "day_off"))
async def set_day_off(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    data = await state.get_data()
    weekday = data["weekday"]
    tz = await get_master_tz(session, tenant.master_id)

    conflicts = await _find_day_off_conflicts(session, tz, weekday, tenant=tenant)
    if conflicts:
        lines = "\n".join(
            f"• {to_local(b.start_datetime, tz).strftime('%d.%m %H:%M')} — {escape(b.service_name_snapshot)}"
            for b in conflicts
        )
        await state.set_state(AdminScheduleFSM.confirm_day_off)
        await callback.message.edit_text(
            f"⚠️ На {WEEKDAY_NAMES_RU[weekday].lower()} уже есть подтверждённые записи:\n\n"
            f"{lines}\n\n"
            "Они не будут отменены автоматически и останутся как есть, если сделать день выходным. "
            "Клиентам об этом ничего не придёт — отменить или перенести их нужно будет вручную "
            "в разделе «Записи».\n\n"
            "Всё равно сделать этот день выходным?",
            reply_markup=confirm_keyboard(),
        )
        await callback.answer()
        return

    await _make_day_off(session, weekday, tenant)
    await state.clear()
    await callback.message.edit_text(f"✅ {WEEKDAY_NAMES_RU[weekday]} теперь выходной.")
    await callback.answer()


async def _make_day_off(session: AsyncSession, weekday: int, tenant: TenantContext) -> None:
    """Выходной день: часы работы сбрасываются, а обед (он имеет смысл только внутри рабочего дня) убирается."""
    repo = ScheduleRepository(session, tenant.master_id)
    await repo.upsert_working_hours(weekday, is_working=False, start_time=None, end_time=None)
    await repo.clear_breaks(weekday)
    await session.commit()


@router.callback_query(AdminScheduleFSM.confirm_day_off, ConfirmCallback.filter(F.action == "edit"))
async def cancel_day_off(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    data = await state.get_data()
    weekday = data["weekday"]
    await state.set_state(AdminScheduleFSM.select_start_time)
    await callback.message.edit_text(await _weekday_prompt(session, weekday, tenant=tenant), reply_markup=schedule_start_time_keyboard(_day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes)))
    await callback.answer()


@router.callback_query(AdminScheduleFSM.confirm_day_off, ConfirmCallback.filter(F.action == "yes"))
async def confirm_day_off(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    data = await state.get_data()
    weekday = data["weekday"]
    await _make_day_off(session, weekday, tenant)
    await state.clear()
    await callback.message.edit_text(f"✅ {WEEKDAY_NAMES_RU[weekday]} теперь выходной.")
    await callback.answer()


@router.callback_query(AdminScheduleFSM.select_start_time, TimeCallback.filter())
async def select_start_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    start_dt = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)
    options = [t for t in _day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes) if t.time() > start_dt.time()]
    if not options:
        await callback.answer("Для этого времени начала нет доступного времени окончания.", show_alert=True)
        return

    data = await state.get_data()
    weekday = data["weekday"]
    await state.update_data(start_time=start_dt.time().isoformat())
    await state.set_state(AdminScheduleFSM.select_end_time)
    await callback.message.edit_text(
        f"{WEEKDAY_NAMES_RU[weekday]}\n🕐 Начало: {start_dt.strftime('%H:%M')}\n\n🕐 Выберите время окончания работы:",
        reply_markup=times_keyboard(options, with_main_menu=False),
    )
    await callback.answer()


@router.callback_query(AdminScheduleFSM.select_end_time, NavCallback.filter(F.target == "back"))
async def back_to_start_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    data = await state.get_data()
    weekday = data["weekday"]
    await state.set_state(AdminScheduleFSM.select_start_time)
    await callback.message.edit_text(await _weekday_prompt(session, weekday, tenant=tenant), reply_markup=schedule_start_time_keyboard(_day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes)))
    await callback.answer()


@router.callback_query(AdminScheduleFSM.select_end_time, TimeCallback.filter())
async def select_end_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    end_dt = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)
    data = await state.get_data()
    weekday = data["weekday"]
    start_time = dt.time.fromisoformat(data["start_time"])

    await state.update_data(end_time=end_dt.time().isoformat())
    await state.set_state(AdminScheduleFSM.confirm)
    text = (
        "Сохранить расписание?\n\n"
        f"{WEEKDAY_NAMES_RU[weekday]}: {start_time.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}"
    )
    await callback.message.edit_text(text, reply_markup=confirm_keyboard())
    await callback.answer()


@router.callback_query(AdminScheduleFSM.confirm, ConfirmCallback.filter(F.action == "edit"))
async def edit_schedule_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    data = await state.get_data()
    weekday = data["weekday"]
    await state.set_state(AdminScheduleFSM.select_start_time)
    await callback.message.edit_text(await _weekday_prompt(session, weekday, tenant=tenant), reply_markup=schedule_start_time_keyboard(_day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes)))
    await callback.answer()


@router.callback_query(AdminScheduleFSM.confirm, ConfirmCallback.filter(F.action == "yes"))
async def confirm_schedule(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    data = await state.get_data()
    weekday = data["weekday"]
    start_time = dt.time.fromisoformat(data["start_time"])
    end_time = dt.time.fromisoformat(data["end_time"])

    repo = ScheduleRepository(session, tenant.master_id)
    await repo.upsert_working_hours(weekday, is_working=True, start_time=start_time, end_time=end_time)
    # Обед, который не помещается в новые часы работы, убираем: вне рабочего времени он ничего не значит.
    dropped = await repo.drop_breaks_outside_hours(weekday, start_time, end_time)
    await session.commit()
    await state.clear()
    text = f"✅ {WEEKDAY_NAMES_RU[weekday]}: {start_time.strftime('%H:%M')} — {end_time.strftime('%H:%M')} сохранено."
    if dropped:
        text += "\n🍽 Обед не помещался в новые часы работы, поэтому убран. Задайте его заново в «🍽 Обед»."
    await callback.message.edit_text(text)
    await callback.answer()

# --- Обед (повторяющийся перерыв внутри рабочего дня) ---
# Хранится в таблице breaks (по одному обеду на день недели); расчёт свободного времени уже вычитает перерывы,
# поэтому клиент не видит слотов в обед, а услуга не может «перетечь» через него.


async def _show_lunch_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    weekday = (await state.get_data())["weekday"]
    text, keyboard = await _lunch_screen(session, weekday, tenant=tenant)
    await state.set_state(AdminScheduleFSM.lunch_menu)
    await callback.message.edit_text(text, reply_markup=keyboard)


async def _show_lunch_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> bool:
    """Экран выбора начала обеда. False — выбрать нечего (рабочие часы дня изменились или слишком короткие)."""
    weekday = (await state.get_data())["weekday"]
    work = _working_range(await ScheduleRepository(session, tenant.master_id).get_working_hours(weekday))
    if work is None:
        return False
    tz = await get_master_tz(session, tenant.master_id)
    options = _lunch_start_options(_day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes), work)
    if not options:
        return False
    await state.set_state(AdminScheduleFSM.lunch_start)
    await callback.message.edit_text(
        f"{WEEKDAY_NAMES_RU[weekday]}\n🍽 Выберите время начала обеда:", reply_markup=times_keyboard(options, with_main_menu=False)
    )
    return True


@router.callback_query(AdminScheduleFSM.select_start_time, AdminScheduleCallback.filter(F.action == "lunch"))
async def open_lunch(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    weekday = (await state.get_data())["weekday"]
    if _working_range(await ScheduleRepository(session, tenant.master_id).get_working_hours(weekday)) is None:
        await callback.answer("Сначала задайте часы работы этого дня: обед должен быть внутри них.", show_alert=True)
        return
    await _show_lunch_menu(callback, state, session, tenant)
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_menu, NavCallback.filter(F.target == "back"))
async def back_from_lunch_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    weekday = (await state.get_data())["weekday"]
    await state.set_state(AdminScheduleFSM.select_start_time)
    await callback.message.edit_text(
        await _weekday_prompt(session, weekday, tenant=tenant),
        reply_markup=schedule_start_time_keyboard(_day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes)),
    )
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_menu, AdminScheduleCallback.filter(F.action == "lunch_set"))
async def start_lunch_setup(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    if not await _show_lunch_start(callback, state, session, tenant):
        await callback.answer("Для этого дня нельзя задать обед: проверьте часы работы.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_menu, AdminScheduleCallback.filter(F.action == "lunch_remove"))
async def remove_lunch(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    weekday = (await state.get_data())["weekday"]
    await ScheduleRepository(session, tenant.master_id).clear_breaks(weekday)
    await session.commit()
    await state.clear()
    await callback.message.edit_text(f"✅ {WEEKDAY_NAMES_RU[weekday]}: обед убран.")
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_start, NavCallback.filter(F.target == "back"))
async def back_from_lunch_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_lunch_menu(callback, state, session, tenant)
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_start, TimeCallback.filter())
async def select_lunch_start(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    weekday = (await state.get_data())["weekday"]
    work = _working_range(await ScheduleRepository(session, tenant.master_id).get_working_hours(weekday))
    tz = await get_master_tz(session, tenant.master_id)
    start_dt = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)
    grid = _day_time_grid(tz, (await tenant.get_settings()).slot_interval_minutes)
    # Время приходит из кнопки, но проверяем его заново: кнопка могла устареть или быть подделана.
    if work is None or not (work[0] <= start_dt.time() < work[1]) or not _lunch_end_options(grid, start_dt.time(), work):
        await callback.answer("Это время недоступно. Выберите другое.", show_alert=True)
        return
    await state.update_data(lunch_start=start_dt.time().isoformat())
    await state.set_state(AdminScheduleFSM.lunch_end)
    await callback.message.edit_text(
        f"{WEEKDAY_NAMES_RU[weekday]}\n🍽 Начало обеда: {start_dt.strftime('%H:%M')}\n\n🍽 Выберите время окончания обеда:",
        reply_markup=times_keyboard(_lunch_end_options(grid, start_dt.time(), work), with_main_menu=False),
    )
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_end, NavCallback.filter(F.target == "back"))
async def back_from_lunch_end(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    if not await _show_lunch_start(callback, state, session, tenant):
        await _show_lunch_menu(callback, state, session, tenant)
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_end, TimeCallback.filter())
async def select_lunch_end(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    data = await state.get_data()
    weekday = data["weekday"]
    start = dt.time.fromisoformat(data["lunch_start"])
    work = _working_range(await ScheduleRepository(session, tenant.master_id).get_working_hours(weekday))
    tz = await get_master_tz(session, tenant.master_id)
    end_dt = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)
    if work is None or not (start < end_dt.time() <= work[1]):
        await callback.answer("Это время недоступно. Выберите другое.", show_alert=True)
        return
    await state.update_data(lunch_end=end_dt.time().isoformat())
    await state.set_state(AdminScheduleFSM.lunch_confirm)
    await callback.message.edit_text(
        f"Сохранить обед?\n\n{WEEKDAY_NAMES_RU[weekday]}: {start.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}\n\n"
        "Можно сохранить только для этого дня или сразу для всех рабочих дней, куда он помещается.",
        reply_markup=schedule_lunch_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_confirm, ConfirmCallback.filter(F.action == "edit"))
async def edit_lunch(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    if not await _show_lunch_start(callback, state, session, tenant):
        await _show_lunch_menu(callback, state, session, tenant)
    await callback.answer()


async def _save_lunch(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext, *, all_days: bool
) -> None:
    data = await state.get_data()
    weekday = data["weekday"]
    start = dt.time.fromisoformat(data["lunch_start"])
    end = dt.time.fromisoformat(data["lunch_end"])
    repo = ScheduleRepository(session, tenant.master_id)
    week_hours = await _load_week_hours(session, tenant=tenant)

    targets: list[int] = []
    skipped: list[int] = []  # рабочие дни, в рабочие часы которых этот обед не помещается
    for day in range(len(WEEKDAY_NAMES_RU)) if all_days else [weekday]:
        work = _working_range(week_hours[day])
        if work is None:
            continue
        (targets if work[0] <= start and end <= work[1] else skipped).append(day)
    if weekday not in targets:  # часы этого дня успели измениться: обед уже не помещается
        await callback.answer("Обед не помещается в рабочие часы этого дня. Выберите другое время.", show_alert=True)
        return

    for day in targets:
        await repo.set_lunch(day, start, end)
    await session.commit()
    tz = await get_master_tz(session, tenant.master_id)
    conflicts = await _find_lunch_conflicts(session, tz, set(targets), start, end, tenant=tenant)
    await state.clear()

    period = f"{start.strftime('%H:%M')} — {end.strftime('%H:%M')}"
    if all_days:
        text = f"✅ Обед {period} сохранён: {', '.join(WEEKDAY_SHORT_RU[d] for d in targets)}."
        if skipped:
            text += f"\nНе подошёл по рабочим часам: {', '.join(WEEKDAY_SHORT_RU[d] for d in skipped)}."
    else:
        text = f"✅ {WEEKDAY_NAMES_RU[weekday]}: обед {period} сохранён."
    if conflicts:
        text += _conflicts_text(conflicts, tz)
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(AdminScheduleFSM.lunch_confirm, AdminScheduleCallback.filter(F.action == "lunch_day"))
async def save_lunch_one_day(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _save_lunch(callback, state, session, tenant, all_days=False)


@router.callback_query(AdminScheduleFSM.lunch_confirm, AdminScheduleCallback.filter(F.action == "lunch_all"))
async def save_lunch_all_days(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _save_lunch(callback, state, session, tenant, all_days=True)
