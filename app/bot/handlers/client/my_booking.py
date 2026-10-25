import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.calendar_view import calendar_markup, calendar_notice, edit_calendar, has_free_dates
from app.bot.keyboards.callback_data import (
    BookingActionCallback,
    CalendarCallback,
    DateCallback,
    DatesPageCallback,
    NavCallback,
    TimeCallback,
)
from app.bot.keyboards.client_keyboards import (
    cancel_confirm_keyboard,
    my_booking_keyboard,
    times_keyboard,
)
from app.bot.states.booking_states import RescheduleFSM
from app.database.repositories.booking_repository import BookingRepository
from app.bot.middlewares.admin_filter import get_admin_contact_username
from app.database.repositories.service_repository import ServiceRepository
from app.database.repositories.user_repository import UserRepository
from app.services.booking_service import BookingService, booking_service_for
from app.services.exceptions import (
    BookingNotCancellableError,
    BookingNotFoundError,
    ServiceInactiveError,
    ServiceNotFoundError,
    SlotNotAvailableError,
)
from app.services.notifier import MasterNotifier
from app.services.message_texts import booking_template_values, profile_template_values
from app.utils.master_settings import DEFAULT_CURRENCY
from app.utils.default_texts import default_cancellation_text
from app.utils.money import format_money
from app.utils.text_templates import render_template
from app.utils.timezone import format_time_range, get_master_tz, to_local, weekday_name_ru
from app.tenancy import TenantContext

router = Router(name="client_my_booking")

BOOKING_NOT_FOUND_TEXT = "Запись не найдена."
RESCHEDULE_DISABLED_TEXT = "Мастер отключил перенос записей. Если нужно другое время, свяжитесь с мастером."


async def _booking_from_fsm(session: AsyncSession, tenant: TenantContext, data: dict, telegram_id: int):
    """Запись переноса из FSM-данных. Данные состояния на веру не берём: запись перепроверяется так же, как при
    нажатии кнопки, — она должна принадлежать ЭТОМУ пользователю у ЭТОГО мастера (иначе None).
    Для состояния без booking_id тоже None."""
    booking_id = data.get("booking_id")
    if booking_id is None:
        return None
    return await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(booking_id, telegram_id)


def _format_booking(b, tz: dt.tzinfo, currency: str = DEFAULT_CURRENCY) -> str:
    start_local = to_local(b.start_datetime, tz)
    return (
        "📋 Ваша запись\n\n"
        f"{escape(b.service_name_snapshot)}\n\n"
        f"📅 {start_local.strftime('%d.%m.%Y')} - {weekday_name_ru(start_local)}\n"
        f"🕐 {format_time_range(b.start_datetime, b.end_datetime, tz)}\n\n"
        f"💰 {format_money(b.service_price_snapshot, currency)}\n\n"
        "Статус: ✅ Подтверждена"
    )


@router.message(F.text == "📋 Мои записи")
async def show_my_booking(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    user_repo = UserRepository(session, tenant.master_id)
    user = await user_repo.get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer("У вас пока нет записей.")
        return

    tz = await get_master_tz(session, tenant.master_id)
    booking_repo = BookingRepository(session, tenant.master_id)
    upcoming = await booking_repo.get_user_upcoming(user.id, dt.datetime.now(tz))
    if not upcoming:
        await message.answer("⚠️ У вас пока нет активных записей.")
        return

    contact_username = await get_admin_contact_username(message.bot, tenant.master)
    master_settings = await tenant.get_settings()

    for b in upcoming:
        await message.answer(
            _format_booking(b, tz, master_settings.currency),
            reply_markup=my_booking_keyboard(b.id, contact_username, master_settings.allow_reschedule),
        )


@router.callback_query(BookingActionCallback.filter(F.action == "cancel"))
async def ask_cancel_confirmation(
    callback: CallbackQuery, callback_data: BookingActionCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    booking = await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(callback_data.booking_id, callback.from_user.id)
    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    start_local = to_local(booking.start_datetime, tz)
    await callback.message.edit_text(
        "❓ Вы действительно хотите отменить запись?\n\n"
        f"📅 {start_local.strftime('%d.%m.%Y')} - {weekday_name_ru(start_local)}\n"
        f"🕐 {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n"
        f"{escape(booking.service_name_snapshot)}",
        reply_markup=cancel_confirm_keyboard(booking.id),
    )
    await callback.answer()


@router.callback_query(BookingActionCallback.filter(F.action == "cancel_back"))
async def cancel_back(callback: CallbackQuery, callback_data: BookingActionCallback, session: AsyncSession, tenant: TenantContext) -> None:
    booking = await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(callback_data.booking_id, callback.from_user.id)
    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    contact_username = await get_admin_contact_username(callback.bot, tenant.master)
    master_settings = await tenant.get_settings()
    await callback.message.edit_text(
        _format_booking(booking, tz, master_settings.currency),
        reply_markup=my_booking_keyboard(booking.id, contact_username, master_settings.allow_reschedule),
    )
    await callback.answer()


@router.callback_query(BookingActionCallback.filter(F.action == "cancel_confirm"))
async def confirm_cancel(callback: CallbackQuery, callback_data: BookingActionCallback, session: AsyncSession, tenant: TenantContext) -> None:
    owned = await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(callback_data.booking_id, callback.from_user.id)
    if owned is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)
    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(callback.from_user.id)

    try:
        booking = await booking_service.cancel_booking(owned.id, by_client=True)
        await session.commit()
    except BookingNotCancellableError:
        await session.rollback()
        await callback.answer("Эту запись уже нельзя отменить.", show_alert=True)
        return

    cancelled_local = to_local(booking.start_datetime, tz)
    master_settings = await tenant.get_settings()
    currency = master_settings.currency

    def default_cancellation() -> str:
        return default_cancellation_text(
            booking.service_name_snapshot,
            cancelled_local.strftime("%d.%m.%Y"),
            weekday_name_ru(cancelled_local),
            format_time_range(booking.start_datetime, booking.end_datetime, tz),
            format_money(booking.service_price_snapshot, currency),
        )

    values = None
    if master_settings.cancellation_text is not None:
        address, master_name = await profile_template_values(session, tenant.master_id)
        values = booking_template_values(
            booking, tz, client_name=user.first_name, address=address, master_name=master_name, currency=currency
        )
    await callback.message.edit_text(
        render_template(master_settings.cancellation_text, values or {}, default_cancellation, context="cancellation")
    )
    await callback.answer()

    notifier = MasterNotifier(callback.bot, master_id=tenant.master_id, owner_telegram_id=tenant.master.owner_telegram_id, session=tenant.session)
    await notifier.notify_cancelled_booking(booking, user, tz, currency)

    # Мастеру — предупреждение, он решает, что делать с доступом. Два случая:
    #  1) три и более самостоятельных записи подряд отменены клиентом (в тексте реальное число);
    #  2) у клиента было больше трёх записей (разрешение мастера «сверх лимита») и он отменил три любых из них.
    streak = await booking_service.cancellation_streak(user)
    over_limit_cancels = 0
    if booking.manual_client_name is None:  # записи, созданные мастером вручную, в счёт клиента не идут
        over_limit_cancels = await booking_service.note_over_limit_cancellation(user)
    streak_limit = master_settings.cancel_streak_limit  # порог серии отмен — настройка мастера
    if streak >= streak_limit or over_limit_cancels >= streak_limit:
        completed = await booking_service.bookings.count_completed_for_user(user.id)
        if streak >= streak_limit:
            await notifier.notify_cancellation_streak(user, completed, streak)
        else:
            await notifier.notify_cancellation_streak(
                user, completed, over_limit_cancels, over_limit=True, max_active_bookings=master_settings.max_active_bookings
            )
        # Счёт начинается заново: если мастер не отреагирует, следующее предупреждение придёт после новых трёх отмен.
        client = await booking_service.users.get_client(user.id)
        if client is not None:
            client.cancel_streak_reset_at = dt.datetime.now(dt.timezone.utc)
            client.over_limit_cancels = None
    await session.commit()


# --- Перенос ---


@router.callback_query(BookingActionCallback.filter(F.action == "reschedule"))
async def start_reschedule(
    callback: CallbackQuery, callback_data: BookingActionCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    if not (await tenant.get_settings()).allow_reschedule:
        await callback.answer(RESCHEDULE_DISABLED_TEXT, show_alert=True)
        return
    booking = await BookingRepository(session, tenant.master_id).get_owned_by_telegram_id(callback_data.booking_id, callback.from_user.id)
    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    if not BookingService.is_modifiable(booking, dt.datetime.now(tz)):
        await callback.answer("Эту запись уже нельзя перенести.", show_alert=True)
        return
    if not await has_free_dates(session, tenant, tz, booking.occupied_minutes):
        await callback.answer("Свободных дат пока нет.", show_alert=True)
        return

    await state.update_data(booking_id=booking.id, service_duration=booking.occupied_minutes)
    await state.set_state(RescheduleFSM.select_date)
    await show_reschedule_calendar(callback, session, tenant, tz, booking.occupied_minutes, dt.datetime.now(tz).date())


async def show_reschedule_calendar(
    callback: CallbackQuery,
    session: AsyncSession,
    tenant: TenantContext,
    tz: dt.tzinfo,
    duration: int,
    requested_month: dt.date,
) -> None:
    """Календарь выбора новой даты при переносе: свободные для записи дни, занятые — 🔴, выходные — ➖."""
    markup = await calendar_markup(session, tenant, tz, requested_month, duration=duration)
    await edit_calendar(callback, "📅 Выберите новую дату:", markup)


# Обработчики календаря работают и при переносе из «Моих записей», и при переносе из напоминания:
# у обоих одно и то же состояние RescheduleFSM.select_date.
@router.callback_query(RescheduleFSM.select_date, CalendarCallback.filter(F.action == "month"))
async def reschedule_change_month(
    callback: CallbackQuery, callback_data: CalendarCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    try:
        requested_month = dt.date(callback_data.year, callback_data.month, 1)
    except ValueError:
        await callback.answer()
        return
    data = await state.get_data()
    tz = await get_master_tz(session, tenant.master_id)
    await show_reschedule_calendar(callback, session, tenant, tz, data["service_duration"], requested_month)


router.callback_query.register(
    calendar_notice, RescheduleFSM.select_date, CalendarCallback.filter(F.action.in_({"noop", "busy", "off"}))
)


@router.callback_query(RescheduleFSM.select_date, DatesPageCallback.filter())
async def reschedule_old_dates_page(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    """«Раньше»/«Дальше» из сообщения со старой сеткой дат (отправлено до перехода на календарь): показываем календарь."""
    data = await state.get_data()
    tz = await get_master_tz(session, tenant.master_id)
    await show_reschedule_calendar(callback, session, tenant, tz, data["service_duration"], dt.datetime.now(tz).date())


@router.callback_query(RescheduleFSM.select_date, CalendarCallback.filter(F.action == "cancel"))
async def reschedule_cancel_calendar(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    """«✖ Отмена» на выборе даты — отказ от переноса: возвращаем карточку записи."""
    await reschedule_back_to_booking(callback, state, session, tenant)


@router.callback_query(RescheduleFSM.select_date, NavCallback.filter(F.target == "back"))
async def reschedule_back_to_booking(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    """«Назад» на выборе даты — отказ от переноса: возвращаем карточку записи."""
    data = await state.get_data()
    await state.clear()
    booking = await _booking_from_fsm(session, tenant, data, callback.from_user.id)
    if booking is None:
        await callback.answer(BOOKING_NOT_FOUND_TEXT, show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    contact_username = await get_admin_contact_username(callback.bot, tenant.master)
    master_settings = await tenant.get_settings()
    await callback.message.edit_text(
        _format_booking(booking, tz, master_settings.currency),
        reply_markup=my_booking_keyboard(booking.id, contact_username, master_settings.allow_reschedule),
    )
    await callback.answer()


@router.callback_query(RescheduleFSM.select_time, NavCallback.filter(F.target == "back"))
async def reschedule_back_to_dates(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    """«Назад» на выборе времени — к календарю, на месяц выбранной даты."""
    data = await state.get_data()
    tz = await get_master_tz(session, tenant.master_id)
    selected = data.get("selected_date")
    await state.set_state(RescheduleFSM.select_date)
    await show_reschedule_calendar(
        callback,
        session,
        tenant,
        tz,
        data["service_duration"],
        dt.date.fromisoformat(selected) if selected else dt.datetime.now(tz).date(),
    )


@router.callback_query(RescheduleFSM.select_date, DateCallback.filter())
async def reschedule_select_date(
    callback: CallbackQuery, callback_data: DateCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    data = await state.get_data()
    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)
    selected_date = dt.date.fromisoformat(callback_data.date)
    now = dt.datetime.now(tz)
    slots = await booking_service.get_available_slots(
        selected_date, data["service_duration"], now=now if selected_date == now.date() else None
    )
    if not slots:
        await callback.answer("На эту дату нет свободного времени.", show_alert=True)
        return

    await state.update_data(selected_date=selected_date.isoformat())
    await state.set_state(RescheduleFSM.select_time)
    await callback.message.edit_text(
        f"📅 {selected_date.strftime('%d.%m.%Y')}\n\n🕐 Свободное время:", reply_markup=times_keyboard(slots)
    )
    await callback.answer()


@router.callback_query(RescheduleFSM.select_time, TimeCallback.filter())
async def reschedule_select_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    data = await state.get_data()
    if not (await tenant.get_settings()).allow_reschedule:
        # Мастер отключил перенос, пока клиент выбирал время: сценарий завершается, запись остаётся как была.
        await state.clear()
        await callback.answer(RESCHEDULE_DISABLED_TEXT, show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)
    user_repo = UserRepository(session, tenant.master_id)
    user = await user_repo.get_by_telegram_id(callback.from_user.id)
    old_booking = await _booking_from_fsm(session, tenant, data, callback.from_user.id)
    if old_booking is None:
        # Запись из состояния недоступна (удалена, чужая, состояние неполное): сценарий завершается без изменений.
        await state.clear()
        await callback.answer(BOOKING_NOT_FOUND_TEXT, show_alert=True)
        return
    new_start = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)

    try:
        new_booking = await booking_service.reschedule_booking(old_booking.id, new_start)
        await session.commit()
    except SlotNotAvailableError:
        await session.rollback()
        await callback.message.edit_text("❌ Это время только что заняли. Ваша старая запись осталась в силе.")
        await callback.answer()
        await state.clear()
        return
    except (ServiceInactiveError, ServiceNotFoundError):
        await session.rollback()
        await callback.message.edit_text(
            "❌ Эта услуга больше недоступна для записи. Ваша старая запись осталась в силе — "
            "чтобы выбрать другое время, свяжитесь с мастером."
        )
        await callback.answer()
        await state.clear()
        return
    except (BookingNotCancellableError, BookingNotFoundError):
        await session.rollback()
        await callback.message.edit_text("Эту запись уже нельзя перенести.")
        await callback.answer()
        await state.clear()
        return

    new_start_local = to_local(new_booking.start_datetime, tz)
    master_settings = await tenant.get_settings()
    contact_username = await get_admin_contact_username(callback.bot, tenant.master)
    await callback.message.edit_text(
        "✅ Запись перенесена!\n\n"
        f"{escape(new_booking.service_name_snapshot)}\n"
        f"📅 {new_start_local.strftime('%d.%m.%Y')} - {weekday_name_ru(new_start_local)}\n"
        f"🕐 {format_time_range(new_booking.start_datetime, new_booking.end_datetime, tz)}\n"
        f"💰 {format_money(new_booking.service_price_snapshot, master_settings.currency)}",
        reply_markup=my_booking_keyboard(new_booking.id, contact_username, master_settings.allow_reschedule),
    )
    await callback.answer()
    await state.clear()

    notifier = MasterNotifier(callback.bot, master_id=tenant.master_id, owner_telegram_id=tenant.master.owner_telegram_id, session=tenant.session)
    await notifier.notify_rescheduled_booking(old_booking, new_booking, user, tz, master_settings.currency)