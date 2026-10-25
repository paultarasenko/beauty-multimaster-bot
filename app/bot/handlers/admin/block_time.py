import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.calendar_view import CALENDAR_NOTICES, calendar_markup, calendar_text, edit_calendar, has_working_days
from app.bot.keyboards.admin_keyboards import (
    BOOKINGS_MENU_PROMPT,
    BTN_BOOKINGS_BLOCK,
    BTN_BOOKINGS_UNBLOCK,
    admin_bookings_menu_keyboard,
    blocks_to_unblock_keyboard,
    unblock_confirm_keyboard,
)
from app.bot.keyboards.callback_data import (
    AdminBlockCallback,
    CalendarCallback,
    ConfirmCallback,
    DateCallback,
    DatesPageCallback,
    NavCallback,
    TimeCallback,
)
from app.bot.keyboards.client_keyboards import confirm_keyboard, times_keyboard
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminBlockTimeFSM
from app.database.models import BlockedPeriod
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.schedule_repository import ScheduleRepository
from app.utils.timezone import get_master_tz, to_local
from app.tenancy import TenantContext

router = Router(name="admin_block_time")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

MAX_UNBLOCK_LIST = 10  # сколько ближайших блокировок показывать в «Разблокировать время»
MAX_REASON_IN_LABEL = 30  # у блокировок, созданных раньше, может быть причина — на кнопке её обрезаем

BLOCK_DATE_HEADER = "📅 Выберите дату блокировки"


# --- Выбор даты/времени (тот же принцип кнопок, что и в клиентской записи) ---


def _time_grid(start_time: dt.time, end_time: dt.time, step_minutes: int) -> list[dt.time]:
    """Сетка времени с шагом step_minutes от start_time до end_time включительно."""
    anchor = dt.date.today()
    cur = dt.datetime.combine(anchor, start_time)
    end = dt.datetime.combine(anchor, end_time)
    step = dt.timedelta(minutes=step_minutes)
    grid: list[dt.time] = []
    while cur <= end:
        grid.append(cur.time())
        cur += step
    return grid


async def _show_block_calendar(
    callback: CallbackQuery, session: AsyncSession, tz: dt.tzinfo, month: dt.date, tenant: TenantContext
) -> None:
    await edit_calendar(callback, BLOCK_DATE_HEADER, await calendar_markup(session, tenant, tz, month))


async def _start_time_options(
    session: AsyncSession, tz: dt.tzinfo, date: dt.date, tenant: TenantContext
) -> list[dt.datetime]:
    """Все времена начала блокировки с шагом slot_interval_minutes в пределах рабочего дня,
    без прошедших (если дата — сегодня)."""
    working_hours = await ScheduleRepository(session, tenant.master_id).get_working_hours_for_date(date)
    if not working_hours or not working_hours.is_working or not working_hours.start_time or not working_hours.end_time:
        return []
    grid = _time_grid(working_hours.start_time, working_hours.end_time, (await tenant.get_settings()).slot_interval_minutes)
    now = dt.datetime.now(tz)
    options = []
    for t in grid:
        if t >= working_hours.end_time:  # начало блокировки не может совпадать с концом рабочего дня
            continue
        candidate = dt.datetime.combine(date, t, tzinfo=tz)
        if candidate < now:
            continue
        options.append(candidate)
    return options


async def _end_time_options(
    session: AsyncSession, tz: dt.tzinfo, date: dt.date, start_time: dt.time, tenant: TenantContext
) -> list[dt.datetime]:
    """Все времена окончания блокировки строго позже start_time, вплоть до конца рабочего дня включительно."""
    working_hours = await ScheduleRepository(session, tenant.master_id).get_working_hours_for_date(date)
    if not working_hours or not working_hours.is_working or not working_hours.start_time or not working_hours.end_time:
        return []
    grid = _time_grid(working_hours.start_time, working_hours.end_time, (await tenant.get_settings()).slot_interval_minutes)
    return [dt.datetime.combine(date, t, tzinfo=tz) for t in grid if t > start_time]


@router.message(F.text == BTN_BOOKINGS_BLOCK)
async def start_block_time(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    if not await has_working_days(session, tenant, tz):
        await message.answer("У мастера нет рабочих дней — закрывать нечего.")
        return
    await state.set_state(AdminBlockTimeFSM.select_date)
    await message.answer(
        calendar_text(BLOCK_DATE_HEADER), reply_markup=await calendar_markup(session, tenant, tz, dt.datetime.now(tz).date())
    )


@router.callback_query(AdminBlockTimeFSM.select_date, CalendarCallback.filter(F.action == "month"))
async def change_block_month(
    callback: CallbackQuery, callback_data: CalendarCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    try:
        requested_month = dt.date(callback_data.year, callback_data.month, 1)
    except ValueError:
        await callback.answer()
        return
    tz = await get_master_tz(session, tenant.master_id)
    await _show_block_calendar(callback, session, tz, requested_month, tenant=tenant)


@router.callback_query(AdminBlockTimeFSM.select_date, CalendarCallback.filter(F.action.in_({"noop", "busy", "off"})))
async def block_calendar_notice(callback: CallbackQuery, callback_data: CalendarCallback) -> None:
    """Занятый день и выходной отвечают всплывающим сообщением; остальные неактивные ячейки — молча."""
    notice = CALENDAR_NOTICES.get(callback_data.action)
    await callback.answer(notice, show_alert=notice is not None)


@router.callback_query(AdminBlockTimeFSM.select_date, CalendarCallback.filter(F.action == "cancel"))
async def cancel_block_calendar(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@router.callback_query(AdminBlockTimeFSM.select_date, DatesPageCallback.filter())
async def old_block_dates_page(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    """«Раньше»/«Дальше» из сообщения со старой сеткой дат (отправлено до перехода на календарь): показываем календарь."""
    tz = await get_master_tz(session, tenant.master_id)
    await _show_block_calendar(callback, session, tz, dt.datetime.now(tz).date(), tenant=tenant)


@router.callback_query(AdminBlockTimeFSM.select_date, DateCallback.filter())
async def select_block_date(
    callback: CallbackQuery, callback_data: DateCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    selected_date = dt.date.fromisoformat(callback_data.date)
    options = await _start_time_options(session, tz, selected_date, tenant=tenant)
    if not options:
        await callback.answer("На эту дату уже нет доступного времени.", show_alert=True)
        return

    await state.update_data(block_date=selected_date.isoformat())
    await state.set_state(AdminBlockTimeFSM.select_start_time)
    await callback.message.edit_text(
        f"📅 {selected_date.strftime('%d.%m.%Y')}\n\n🕐 Выберите время начала блокировки",
        reply_markup=times_keyboard(options, with_main_menu=False),
    )
    await callback.answer()


@router.callback_query(AdminBlockTimeFSM.select_date, NavCallback.filter(F.target == "back"))
async def back_to_admin_menu_from_block_dates(callback: CallbackQuery, state: FSMContext) -> None:
    """select_date — первый шаг флоу «Закрыть время», предыдущего шага внутри FSM нет.
    Клавиатура дат (dates_keyboard) общая с клиентской записью и всегда рисует «⬅️ Назад»,
    поэтому колбэк нужно ловить явно — иначе он проваливается в общий stale_back_fallback
    и ошибочно показывает «Сессия устарела».

    Заходят в этот флоу из подменю «Записи», поэтому и возвращать нужно туда же
    (admin_bookings_menu_keyboard), а не в корневое админ-меню."""
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(BOOKINGS_MENU_PROMPT, reply_markup=admin_bookings_menu_keyboard())
    await callback.answer()


@router.callback_query(AdminBlockTimeFSM.select_start_time, NavCallback.filter(F.target == "back"))
async def back_to_block_dates(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    data = await state.get_data()
    await state.set_state(AdminBlockTimeFSM.select_date)
    await _show_block_calendar(callback, session, tz, dt.date.fromisoformat(data["block_date"]), tenant=tenant)


@router.callback_query(AdminBlockTimeFSM.select_start_time, TimeCallback.filter())
async def select_block_start_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    data = await state.get_data()
    block_date = dt.date.fromisoformat(data["block_date"])
    start_dt = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)

    options = await _end_time_options(session, tz, block_date, start_dt.time(), tenant=tenant)
    if not options:
        await callback.answer("Нет доступного времени окончания для этого начала.", show_alert=True)
        return

    await state.update_data(block_start=start_dt.isoformat())
    await state.set_state(AdminBlockTimeFSM.select_end_time)
    await callback.message.edit_text(
        f"📅 {block_date.strftime('%d.%m.%Y')}\n🕐 Начало: {start_dt.strftime('%H:%M')}\n\n"
        "🕐 Выберите время окончания блокировки",
        reply_markup=times_keyboard(options, with_main_menu=False),
    )
    await callback.answer()


@router.callback_query(AdminBlockTimeFSM.select_end_time, NavCallback.filter(F.target == "back"))
async def back_to_block_start_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    data = await state.get_data()
    block_date = dt.date.fromisoformat(data["block_date"])
    options = await _start_time_options(session, tz, block_date, tenant=tenant)
    if not options:
        await callback.answer("На эту дату уже нет доступного времени.", show_alert=True)
        await state.set_state(AdminBlockTimeFSM.select_date)
        await _show_block_calendar(callback, session, tz, block_date, tenant=tenant)
        return

    await state.set_state(AdminBlockTimeFSM.select_start_time)
    await callback.message.edit_text(
        f"📅 {block_date.strftime('%d.%m.%Y')}\n\n🕐 Выберите время начала блокировки",
        reply_markup=times_keyboard(options, with_main_menu=False),
    )
    await callback.answer()


@router.callback_query(AdminBlockTimeFSM.select_end_time, TimeCallback.filter())
async def select_block_end_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    data = await state.get_data()
    start_dt = dt.datetime.fromisoformat(data["block_start"])
    end_dt = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)

    await state.update_data(block_end=end_dt.isoformat())
    await state.set_state(AdminBlockTimeFSM.confirm)
    text = (
        "Заблокировать это время?\n\n"
        f"📅 {start_dt.strftime('%d.%m.%Y')}\n"
        f"🕐 {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
    )
    await callback.message.edit_text(text, reply_markup=confirm_keyboard())
    await callback.answer()


@router.callback_query(AdminBlockTimeFSM.confirm, ConfirmCallback.filter(F.action == "edit"))
async def edit_block_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    if not await has_working_days(session, tenant, tz):
        await callback.message.edit_text("У мастера нет рабочих дней — закрывать нечего.")
        await callback.answer()
        await state.clear()
        return
    await state.set_state(AdminBlockTimeFSM.select_date)
    await _show_block_calendar(callback, session, tz, dt.datetime.now(tz).date(), tenant=tenant)


@router.callback_query(AdminBlockTimeFSM.confirm, ConfirmCallback.filter(F.action == "yes"))
async def confirm_block_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    data = await state.get_data()
    start_dt = dt.datetime.fromisoformat(data["block_start"])
    end_dt = dt.datetime.fromisoformat(data["block_end"])

    # Баг: раньше блокировка ставилась без проверки на существующие записи клиентов —
    # можно было "закрыть" время поверх подтверждённой записи, и она просто оставалась
    # висеть как ни в чём не бывало. Перед созданием блокировки проверяем пересечение
    # и, если есть live-записи, не даём заблокировать — сначала их нужно отменить/перенести.
    conflicting = await BookingRepository(session, tenant.master_id).get_confirmed_in_range(start_dt, end_dt)
    if conflicting:
        tz = await get_master_tz(session, tenant.master_id)
        lines = "\n".join(
            f"• {to_local(b.start_datetime, tz).strftime('%H:%M')}–{to_local(b.end_datetime, tz).strftime('%H:%M')} "
            f"— {escape(b.service_name_snapshot)}"
            for b in conflicting
        )
        retry_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Изменить время", callback_data=ConfirmCallback(action="edit").pack())]
            ]
        )
        await callback.message.edit_text(
            "⚠️ На это время уже есть подтверждённые записи, заблокировать нельзя:\n\n"
            f"{lines}\n\n"
            "Сначала отмените или перенесите эти записи в разделе «Записи», потом можно будет закрыть время.",
            reply_markup=retry_keyboard,
        )
        await callback.answer("Есть пересекающиеся записи", show_alert=True)
        return

    await ScheduleRepository(session, tenant.master_id).add_blocked_period(start_dt, end_dt, None)
    await session.commit()
    await state.clear()
    text = (
        "✅ Время успешно заблокировано\n\n"
        f"📅 {start_dt.strftime('%d.%m.%Y')}\n"
        f"🕐 {start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
    )
    await callback.message.edit_text(text)
    await callback.answer()


# --- Разблокировать время ---


def _unblock_button_label(block: BlockedPeriod, tz: dt.tzinfo) -> str:
    start_local = to_local(block.start_datetime, tz)
    end_local = to_local(block.end_datetime, tz)
    label = f"🔓 {start_local.strftime('%d.%m')} {start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
    if block.reason:
        reason = block.reason if len(block.reason) <= MAX_REASON_IN_LABEL else block.reason[:MAX_REASON_IN_LABEL] + "…"
        label = f"{label} — {reason}"
    return label


async def _build_unblock_list(session: AsyncSession, tenant: TenantContext) -> tuple[str, InlineKeyboardMarkup | None]:
    tz = await get_master_tz(session, tenant.master_id)
    now = dt.datetime.now(tz)
    blocks = await ScheduleRepository(session, tenant.master_id).get_upcoming_blocked_periods(now, MAX_UNBLOCK_LIST)
    if not blocks:
        return "Активных блокировок нет.", None

    items = [(b.id, _unblock_button_label(b, tz)) for b in blocks]
    return "Выберите блокировку, которую нужно снять", blocks_to_unblock_keyboard(items)


@router.message(F.text == BTN_BOOKINGS_UNBLOCK)
async def open_unblock_list(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await state.clear()  # если мастер не дописал ввод блокировки и нажал другую кнопку — сбрасываем незаконченный ввод
    text, keyboard = await _build_unblock_list(session, tenant=tenant)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(AdminBlockCallback.filter(F.action == "unblock"))
async def ask_unblock_confirmation(
    callback: CallbackQuery, callback_data: AdminBlockCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    block = await ScheduleRepository(session, tenant.master_id).get_blocked_period(callback_data.block_id)
    if block is None:
        await callback.answer("Эта блокировка уже снята.", show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    start_local = to_local(block.start_datetime, tz)
    end_local = to_local(block.end_datetime, tz)
    text = (
        "Снять эту блокировку?\n\n"
        f"📅 {start_local.strftime('%d.%m.%Y')}\n"
        f"🕐 {start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')}"
    )
    await callback.message.edit_text(text, reply_markup=unblock_confirm_keyboard(block.id))
    await callback.answer()


@router.callback_query(AdminBlockCallback.filter(F.action == "back"))
async def back_to_unblock_list(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    text, keyboard = await _build_unblock_list(session, tenant=tenant)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AdminBlockCallback.filter(F.action == "confirm"))
async def confirm_unblock(callback: CallbackQuery, callback_data: AdminBlockCallback, session: AsyncSession, tenant: TenantContext) -> None:
    repo = ScheduleRepository(session, tenant.master_id)
    block = await repo.get_blocked_period(callback_data.block_id)
    if block is None:
        await callback.answer("Эта блокировка уже снята.", show_alert=True)
        return

    await repo.delete_blocked_period(block)
    await session.commit()
    await callback.message.edit_text("✅ Блокировка снята.")
    await callback.answer()