import datetime as dt

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.calendar_view import CALENDAR_NOTICES, calendar_markup, calendar_text, edit_calendar, has_working_days
from app.bot.keyboards.callback_data import (
    CalendarCallback,
    DateCallback,
    DatesPageCallback,
    NavCallback,
    ScheduleBookCallback,
)
from app.bot.keyboards.client_keyboards import (
    booking_day_choice_keyboard,
    info_menu_keyboard,
    start_booking_inline_keyboard,
)
from app.bot.states.booking_states import CheckTimeFSM
from app.services.booking_service import booking_service_for
from app.utils.schedule_image import render_day_schedule_image
from app.utils.timezone import get_master_tz
from app.tenancy import TenantContext, is_owner

router = Router(name="client_check_time")

CALENDAR_HEADER = "🕐 Выберите дату"


@router.message(F.text == "🕒 Проверить время")
async def start_check_time(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    if not await has_working_days(session, tenant, tz):
        await message.answer("Рабочие дни пока не настроены.")
        return

    await state.set_state(CheckTimeFSM.select_date)
    await message.answer(
        calendar_text(CALENDAR_HEADER), reply_markup=await calendar_markup(session, tenant, tz, dt.datetime.now(tz).date())
    )


@router.callback_query(CheckTimeFSM.select_date, CalendarCallback.filter(F.action == "month"))
async def change_month(
    callback: CallbackQuery, callback_data: CalendarCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    try:
        requested_month = dt.date(callback_data.year, callback_data.month, 1)
    except ValueError:
        await callback.answer()
        return
    tz = await get_master_tz(session, tenant.master_id)
    await edit_calendar(callback, CALENDAR_HEADER, await calendar_markup(session, tenant, tz, requested_month))


@router.callback_query(CheckTimeFSM.select_date, CalendarCallback.filter(F.action.in_({"noop", "busy", "off"})))
async def calendar_notice(callback: CallbackQuery, callback_data: CalendarCallback) -> None:
    """Занятый день и выходной отвечают всплывающим сообщением; остальные неактивные ячейки — молча."""
    notice = CALENDAR_NOTICES.get(callback_data.action)
    await callback.answer(notice, show_alert=notice is not None)


@router.callback_query(CheckTimeFSM.select_date, CalendarCallback.filter(F.action == "cancel"))
async def cancel_calendar(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@router.callback_query(CheckTimeFSM.select_date, DatesPageCallback.filter())
async def old_dates_page(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    """«Раньше»/«Дальше» из сообщения со старой сеткой дат (отправлено до перехода на календарь): показываем календарь."""
    tz = await get_master_tz(session, tenant.master_id)
    await edit_calendar(callback, CALENDAR_HEADER, await calendar_markup(session, tenant, tz, dt.datetime.now(tz).date()))


@router.callback_query(CheckTimeFSM.select_date, DateCallback.filter())
async def select_date(
    callback: CallbackQuery, callback_data: DateCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)
    selected_date = dt.date.fromisoformat(callback_data.date)

    working_hours, free_intervals, booking_ranges = await booking_service.get_day_schedule(selected_date)
    date_label = selected_date.strftime("%d.%m.%Y")

    # Записей на этот день ещё нет — сообщаем об этом текстом, под которым та же inline-кнопка «Записаться».
    has_valid_hours = (
        working_hours is not None
        and working_hours.is_working
        and working_hours.start_time is not None
        and working_hours.end_time is not None
    )
    if not booking_ranges or not has_valid_hours:
        await callback.message.answer(
            f"📅 {date_label}\nНа этот день пока нет записей.",
            reply_markup=start_booking_inline_keyboard(selected_date),
        )
        await callback.answer()
        return

    image_bytes = render_day_schedule_image(
        selected_date, tz, working_hours.start_time, working_hours.end_time, free_intervals
    )
    await callback.message.answer_photo(
        BufferedInputFile(image_bytes, filename=f"schedule_{selected_date.isoformat()}.png"),
        reply_markup=start_booking_inline_keyboard(selected_date),
    )
    await callback.answer()


@router.callback_query(ScheduleBookCallback.filter(F.action == "ask"))
async def ask_which_day_to_book(callback: CallbackQuery, callback_data: ScheduleBookCallback) -> None:
    """«Записаться» под расписанием: вместо кнопки показываем «Другой день» (слева) и «Этот день» (справа)."""
    try:
        day = dt.date.fromisoformat(callback_data.date)
    except ValueError:
        await callback.answer()
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=booking_day_choice_keyboard(day))
    except TelegramBadRequest as exc:
        # Двойное нажатие: клавиатура уже заменена — это не ошибка.
        if "message is not modified" not in str(exc):
            raise
    await callback.answer()


@router.callback_query(CheckTimeFSM.select_date, NavCallback.filter(F.target == "back"))
async def back_to_info_menu(callback: CallbackQuery, state: FSMContext, tenant: TenantContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "ℹ️ Выберите действие", reply_markup=info_menu_keyboard(is_admin=is_owner(tenant.master, callback.from_user.id))
    )
    await callback.answer()
