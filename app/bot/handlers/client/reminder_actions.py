import datetime as dt
from html import escape
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.calendar_view import has_free_dates
from app.bot.keyboards.callback_data import BookingActionCallback, ReminderActionCallback
from app.bot.keyboards.client_keyboards import cancel_confirm_keyboard
from app.bot.states.booking_states import RescheduleFSM
from app.database.repositories.booking_repository import BookingRepository
from app.database.models import Notification, NotificationStatus, NotificationType
from app.services.booking_service import BookingService
from app.utils.timezone import format_time_range, get_master_tz, to_local, weekday_name_ru
from sqlalchemy import select, update
from app.tenancy import TenantContext

router = Router(name="client_reminder")


@router.callback_query(ReminderActionCallback.filter(F.action == "still_valid"))
async def reminder_still_valid(
    callback: CallbackQuery, callback_data: ReminderActionCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    booking = await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(callback_data.booking_id, callback.from_user.id)
    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    # Запись и так остаётся confirmed — просто отмечаем, что клиент подтвердил, и убираем кнопки.
    await session.execute(
        update(Notification)
        .where(
            Notification.master_id == tenant.master_id,
            Notification.booking_id == callback_data.booking_id,
            # Только 24-часовое напоминание: иначе «Всё в силе» погасило бы ещё не
            # отправленные напоминания за 2 часа.
            Notification.type == NotificationType.REMINDER_24H.value,
        )
        .values(status=NotificationStatus.SENT.value)
    )
    await session.commit()
    await callback.message.edit_text(callback.message.html_text + "\n\n✅ Спасибо, ждём вас!")
    await callback.answer()


@router.callback_query(ReminderActionCallback.filter(F.action == "cancel"))
async def reminder_cancel(callback: CallbackQuery, callback_data: ReminderActionCallback, session: AsyncSession, tenant: TenantContext) -> None:
    booking = await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(callback_data.booking_id, callback.from_user.id)
    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    start_local = to_local(booking.start_datetime, tz)
    await callback.message.edit_text(
        "Вы действительно хотите отменить запись?\n\n"
        f"{start_local.strftime('%d.%m.%Y')} - {weekday_name_ru(start_local)}, {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n"
        f"{escape(booking.service_name_snapshot)}",
        reply_markup=cancel_confirm_keyboard(booking.id),
    )
    await callback.answer()


@router.callback_query(ReminderActionCallback.filter(F.action == "reschedule"))
async def reminder_reschedule(
    callback: CallbackQuery, callback_data: ReminderActionCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    if not (await tenant.get_settings()).allow_reschedule:
        await callback.answer(
            "Мастер отключил перенос записей. Если нужно другое время, свяжитесь с мастером.", show_alert=True
        )
        return
    booking = await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(callback_data.booking_id, callback.from_user.id)
    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    if not BookingService.is_modifiable(booking, dt.datetime.now(tz)):
        await callback.answer("Эту запись уже нельзя перенести.", show_alert=True)
        return
    from app.bot.handlers.client.my_booking import show_reschedule_calendar

    if not await has_free_dates(session, tenant, tz, booking.occupied_minutes):
        await callback.answer("Свободных дат пока нет.", show_alert=True)
        return

    await state.update_data(booking_id=booking.id, service_duration=booking.occupied_minutes)
    await state.set_state(RescheduleFSM.select_date)
    await show_reschedule_calendar(callback, session, tenant, tz, booking.occupied_minutes, dt.datetime.now(tz).date())
