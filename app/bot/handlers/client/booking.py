import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.calendar_view import calendar_markup, calendar_notice, edit_calendar, has_free_dates
from app.bot.keyboards.callback_data import (
    CalendarCallback,
    ConfirmCallback,
    DateCallback,
    DatesPageCallback,
    NavCallback,
    ScheduleBookCallback,
    ServiceCallback,
    TimeCallback,
)
from app.bot.keyboards.client_keyboards import (
    confirm_keyboard,
    limit_reached_keyboard,
    main_menu_keyboard,
    my_booking_keyboard,
    services_keyboard,
    times_keyboard,
)
from app.bot.middlewares.admin_filter import get_admin_contact_username
from app.bot.states.booking_states import BookingFSM
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.service_repository import ServiceRepository
from app.database.repositories.user_repository import UserRepository
from app.services.booking_service import MAX_ACTIVE_BOOKINGS, booking_service_for
from app.services.access_request_service import BLOCKED_TEXT
from app.services.exceptions import (
    BookingLimitReachedError,
    ClientBlockedError,
    ClientRestrictedError,
    ServiceInactiveError,
    ServiceNotFoundError,
    SlotNotAvailableError,
)
from app.services.notifier import MasterNotifier
from app.utils.duration import format_duration
from app.services.message_texts import booking_template_values, profile_template_values
from app.utils.master_settings import client_reminder_phrase
from app.utils.default_texts import default_confirmation_text
from app.utils.money import format_money
from app.utils.text_templates import render_template
from app.utils.timezone import format_date_ru, format_time_range, get_master_tz, to_local, weekday_name_ru
from app.tenancy import TenantContext, is_owner

router = Router(name="client_booking")

RESTRICTED_TEXT = "🚫 К сожалению, запись для вас сейчас недоступна.\n\nПожалуйста, свяжитесь с мастером."


def _active_bookings_word(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "активных записей"
    if count % 10 == 1:
        return "активная запись"
    if 2 <= count % 10 <= 4:
        return "активные записи"
    return "активных записей"


def _plural(count: int, one: str, few: str, many: str) -> str:
    if 11 <= count % 100 <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many


def limit_reached_text(active: int | None = None, by_master: int = 0, limit: int = MAX_ACTIVE_BOOKINGS) -> str:
    """Сообщение о лимите с реальным числом активных записей клиента (по умолчанию — лимит мастера `limit`).

    by_master — записи, созданные мастером вручную: в лимит они не входят, но клиент видит их в «Мои записи».
    Без этого пояснения число в сообщении расходилось бы со списком записей."""
    count = active if active is not None else limit
    if by_master > 0:
        header = (
            f"⚠️ У вас уже {count} "
            f"{_plural(count, 'запись, сделанная', 'записи, сделанные', 'записей, сделанных')} "
            "самостоятельно — это максимум.\n\n"
            f"Ещё {by_master} {_plural(by_master, 'запись', 'записи', 'записей')} создал мастер — "
            "они не учитываются в лимите."
        )
    else:
        header = f"⚠️ У вас уже {count} {_active_bookings_word(count)} — это максимум."
    return (
        f"{header}\n\n"
        "Дождитесь визита или отмените одну из записей, чтобы записаться снова.\n\n"
        "Также вы можете обратиться к мастеру за возможностью подтверждения следующих записей."
    )


async def _booking_block_reason(session: AsyncSession, telegram_id: int, tenant: TenantContext) -> tuple[str, bool] | None:
    """(текст, это_лимит_записей) для клиента, если новую запись ему создавать нельзя (доступ ограничен / лимит
    записей), иначе None."""
    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(telegram_id)
    if user is None:
        return None
    tz = await get_master_tz(session, tenant.master_id)
    try:
        await (await booking_service_for(session, tz, tenant)).ensure_client_can_book(user.id)
    except ClientBlockedError:
        return BLOCKED_TEXT, False
    except ClientRestrictedError:
        return RESTRICTED_TEXT, False
    except BookingLimitReachedError as exc:
        return limit_reached_text(exc.active, exc.by_master, (await tenant.get_settings()).max_active_bookings), True
    return None


async def _show_service_selection(
    send,
    state: FSMContext,
    session: AsyncSession,
    preselected_date: dt.date | None = None,
    telegram_id: int | None = None, *, tenant: TenantContext,
) -> None:
    """Общий старт записи: показывает список услуг. Используется и обычной кнопкой «📅 Записаться»,
    и inline-кнопками под фото расписания (см. check_time.py).

    preselected_date — день, выбранный кнопкой «Этот день»: после выбора услуги дата не спрашивается."""
    if telegram_id is not None and (reason := await _booking_block_reason(session, telegram_id, tenant=tenant)):
        await state.clear()
        text, is_limit = reason
        await send(text, reply_markup=limit_reached_keyboard() if is_limit else None)
        return
    services = await ServiceRepository(session, tenant.master_id).get_active()
    if not services:
        await send("Пока нет доступных услуг для записи.")
        return
    await state.set_state(BookingFSM.select_service)
    await state.update_data(preselected_date=preselected_date.isoformat() if preselected_date else None)
    text = "Выберите услугу"
    if preselected_date is not None:
        text = f"📅 {preselected_date.strftime('%d.%m.%Y')}\n\n{text}"
    await send(text, reply_markup=services_keyboard(services, (await tenant.get_settings()).currency))


async def _show_time_selection(
    callback: CallbackQuery, state: FSMContext, service, selected_date: dt.date, slots: list[dt.datetime]
) -> None:
    """Выбор времени на дату: общий для обычной записи и для кнопки «Этот день»."""
    await state.update_data(selected_date=selected_date.isoformat())
    await state.set_state(BookingFSM.select_time)
    await callback.message.edit_text(
        f"{escape(service.name)}\n"
        f"📅 {selected_date.strftime('%d.%m.%Y')}\n\n"
        f"🕐 Свободное время:",
        reply_markup=times_keyboard(slots, with_main_menu=False),
    )
    await callback.answer()


@router.message(F.text == "📅 Записаться")
async def start_booking(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_service_selection(message.answer, state, session, telegram_id=message.from_user.id, tenant=tenant)


@router.callback_query(NavCallback.filter(F.target == "start_booking"))
async def start_booking_from_callback(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _show_service_selection(callback.message.answer, state, session, telegram_id=callback.from_user.id, tenant=tenant)
    await callback.answer()


@router.callback_query(ScheduleBookCallback.filter(F.action == "day"))
async def start_booking_for_day(
    callback: CallbackQuery, callback_data: ScheduleBookCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    """«Этот день» под расписанием: запись на день, который смотрел клиент — шаг выбора даты пропускается."""
    try:
        day = dt.date.fromisoformat(callback_data.date)
    except ValueError:
        await callback.answer()
        return
    tz = await get_master_tz(session, tenant.master_id)
    if day < dt.datetime.now(tz).date():
        await callback.answer("Эта дата уже прошла. Нажмите «Другой день».", show_alert=True)
        return
    await _show_service_selection(
        callback.message.answer, state, session, preselected_date=day, telegram_id=callback.from_user.id, tenant=tenant
    )
    await callback.answer()


@router.callback_query(BookingFSM.select_service, ServiceCallback.filter())
async def select_service(
    callback: CallbackQuery, callback_data: ServiceCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    service_repo = ServiceRepository(session, tenant.master_id)
    service = await service_repo.get_by_id(callback_data.service_id)
    if service is None or not service.active:
        await callback.answer("Эта услуга больше недоступна.", show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)

    # Пришли по кнопке «Этот день» — сразу показываем время на эту дату, если услуга на неё помещается.
    notice = ""
    preselected = (await state.get_data()).get("preselected_date")
    if preselected:
        await state.update_data(preselected_date=None)  # одноразово: «Назад» и новая запись идут обычным путём
        selected_date = dt.date.fromisoformat(preselected)
        now = dt.datetime.now(tz)
        slots = (
            []
            if selected_date < now.date()
            else await booking_service.get_available_slots(
                selected_date, service.occupied_minutes, now=now if selected_date == now.date() else None
            )
        )
        if slots:
            await state.update_data(service_id=service.id)
            await _show_time_selection(callback, state, service, selected_date, slots)
            return
        notice = f"На {selected_date.strftime('%d.%m.%Y')} для этой услуги уже нет свободного времени.\n\n"

    if not await has_free_dates(session, tenant, tz, service.occupied_minutes):
        await callback.message.edit_text("К сожалению, свободных дат пока нет. Попробуйте позже.")
        await callback.answer()
        return

    await state.update_data(service_id=service.id)
    await state.set_state(BookingFSM.select_date)
    await _show_booking_calendar(callback, session, tenant, tz, service, dt.datetime.now(tz).date(), notice)


async def _show_booking_calendar(
    callback: CallbackQuery,
    session: AsyncSession,
    tenant: TenantContext,
    tz: dt.tzinfo,
    service,
    requested_month: dt.date,
    notice: str = "",
) -> None:
    """Календарь выбора даты для услуги: занятые для этой услуги дни — 🔴, выходные — ➖."""
    markup = await calendar_markup(session, tenant, tz, requested_month, duration=service.occupied_minutes)
    await edit_calendar(callback, f"{notice}{escape(service.name)}\n\n📅 Выберите дату:", markup)


async def _active_service_from_state(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext
):
    data = await state.get_data()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(data["service_id"])
    if service is None or not service.active:
        await callback.answer("Услуга больше недоступна.", show_alert=True)
        return None
    return service


@router.callback_query(BookingFSM.select_date, CalendarCallback.filter(F.action == "month"))
async def change_month(
    callback: CallbackQuery, callback_data: CalendarCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    try:
        requested_month = dt.date(callback_data.year, callback_data.month, 1)
    except ValueError:
        await callback.answer()
        return
    service = await _active_service_from_state(callback, state, session, tenant)
    if service is None:
        return
    tz = await get_master_tz(session, tenant.master_id)
    await _show_booking_calendar(callback, session, tenant, tz, service, requested_month)


router.callback_query.register(
    calendar_notice, BookingFSM.select_date, CalendarCallback.filter(F.action.in_({"noop", "busy", "off"}))
)


@router.callback_query(BookingFSM.select_date, CalendarCallback.filter(F.action == "cancel"))
async def cancel_calendar(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@router.callback_query(BookingFSM.select_date, DatesPageCallback.filter())
async def old_dates_page(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    """«Раньше»/«Дальше» из сообщения со старой сеткой дат (отправлено до перехода на календарь): показываем календарь."""
    service = await _active_service_from_state(callback, state, session, tenant)
    if service is None:
        return
    tz = await get_master_tz(session, tenant.master_id)
    await _show_booking_calendar(callback, session, tenant, tz, service, dt.datetime.now(tz).date())


@router.callback_query(BookingFSM.select_date, DateCallback.filter())
async def select_date(
    callback: CallbackQuery, callback_data: DateCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    data = await state.get_data()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(data["service_id"])
    if service is None or not service.active:
        await callback.answer("Услуга больше недоступна.", show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)
    selected_date = dt.date.fromisoformat(callback_data.date)
    now = dt.datetime.now(tz)
    slots = await booking_service.get_available_slots(
        selected_date, service.occupied_minutes, now=now if selected_date == now.date() else None
    )
    if not slots:
        await callback.answer("На эту дату уже нет свободного времени.", show_alert=True)
        return

    await _show_time_selection(callback, state, service, selected_date, slots)


@router.callback_query(BookingFSM.select_time, TimeCallback.filter())
async def select_time(
    callback: CallbackQuery, callback_data: TimeCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    data = await state.get_data()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(data["service_id"])
    if service is None or not service.active:
        await callback.answer("Услуга больше недоступна.", show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    start_dt = dt.datetime.fromtimestamp(callback_data.ts, tz=tz)
    await state.update_data(selected_time=start_dt.isoformat())
    await state.set_state(BookingFSM.confirm)

    text = (
        "📋 Проверьте запись:\n\n"
        f"📌 Услуга:\n{escape(service.name)}\n\n"
        f"📅 {format_date_ru(start_dt)}\n"
        f"🕐 {format_time_range(start_dt, start_dt + dt.timedelta(minutes=service.duration_minutes))}\n\n"
        f"💰 {format_money(service.price, (await tenant.get_settings()).currency)}\n"
        f"⏱ ~{format_duration(service.duration_minutes)}"
    )
    await callback.message.edit_text(text, reply_markup=confirm_keyboard())
    await callback.answer()


@router.callback_query(BookingFSM.confirm, ConfirmCallback.filter(F.action == "edit"))
async def edit_booking(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    # Возвращаемся к выбору услуги заново — самый простой и предсказуемый способ "изменить".
    services = await ServiceRepository(session, tenant.master_id).get_active()
    await state.set_state(BookingFSM.select_service)
    await callback.message.edit_text(
        "Выберите услугу", reply_markup=services_keyboard(services, (await tenant.get_settings()).currency)
    )
    await callback.answer()


@router.callback_query(BookingFSM.confirm, ConfirmCallback.filter(F.action == "yes"))
async def confirm_booking(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    data = await state.get_data()
    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)
    user_repo = UserRepository(session, tenant.master_id)
    user = await user_repo.get_or_create(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
    )
    start_dt = dt.datetime.fromisoformat(data["selected_time"])

    try:
        booking = await booking_service.create_booking(
            user.id, data["service_id"], start_dt, enforce_client_limits=True
        )
        await session.commit()
    except (ClientBlockedError, ClientRestrictedError, BookingLimitReachedError) as exc:
        await session.rollback()
        if isinstance(exc, ClientBlockedError):
            text = BLOCKED_TEXT
        elif isinstance(exc, ClientRestrictedError):
            text = RESTRICTED_TEXT
        else:
            text = limit_reached_text(exc.active, exc.by_master, (await tenant.get_settings()).max_active_bookings)
        markup = limit_reached_keyboard() if isinstance(exc, BookingLimitReachedError) else None
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()
        await state.clear()
        return
    except SlotNotAvailableError:
        await session.rollback()
        await callback.message.edit_text(
            "❌ К сожалению, это время только что заняли.\n\nВыберите другое время, нажав /start → 📅 Записаться."
        )
        await callback.answer()
        await state.clear()
        return
    except (ServiceInactiveError, ServiceNotFoundError):
        await session.rollback()
        await callback.message.edit_text(
            "❌ Эта услуга больше недоступна для записи.\n\nВыберите другую, нажав /start → 📅 Записаться."
        )
        await callback.answer()
        await state.clear()
        return

    start_local = to_local(booking.start_datetime, tz)
    master_settings = await tenant.get_settings()
    currency = master_settings.currency
    reminder_phrase = client_reminder_phrase(master_settings)

    def default_confirmation() -> str:
        return default_confirmation_text(
            booking.service_name_snapshot,
            start_local.strftime("%d.%m.%Y"),
            weekday_name_ru(start_local),
            format_time_range(booking.start_datetime, booking.end_datetime, tz),
            format_money(booking.service_price_snapshot, currency),
            reminder_phrase,
        )

    values = None
    if master_settings.booking_confirmation_text is not None:
        address, master_name = await profile_template_values(session, tenant.master_id)
        values = booking_template_values(
            booking, tz, client_name=user.first_name, address=address, master_name=master_name, currency=currency
        )
    confirmation = render_template(
        master_settings.booking_confirmation_text, values or {}, default_confirmation, context="booking_confirmation"
    )
    contact_username = await get_admin_contact_username(callback.bot, tenant.master)
    await callback.message.edit_text(
        confirmation,
        reply_markup=my_booking_keyboard(booking.id, contact_username, master_settings.allow_reschedule),
    )
    await callback.answer()
    await state.clear()

    notifier = MasterNotifier(callback.bot, master_id=tenant.master_id, owner_telegram_id=tenant.master.owner_telegram_id, session=tenant.session)
    await notifier.notify_new_booking(booking, user, tz, currency)


@router.callback_query(NavCallback.filter(F.target == "main_menu"))
async def go_to_main_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await state.clear()
    await callback.message.delete()

    tz = await get_master_tz(session, tenant.master_id)
    user_repo = UserRepository(session, tenant.master_id)
    user = await user_repo.get_by_telegram_id(callback.from_user.id)
    has_active_booking = False
    if user is not None:
        upcoming = await BookingRepository(session, tenant.master_id).get_user_upcoming(user.id, dt.datetime.now(tz))
        has_active_booking = len(upcoming) > 0

    await callback.message.answer(
        "Выберите действие",
        reply_markup=main_menu_keyboard(
            has_active_booking=has_active_booking, is_admin=is_owner(tenant.master, callback.from_user.id)
        ),
    )
    await callback.answer()


@router.callback_query(BookingFSM.select_date, NavCallback.filter(F.target == "back"))
async def back_to_services(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    services = await ServiceRepository(session, tenant.master_id).get_active()
    await state.set_state(BookingFSM.select_service)
    await callback.message.edit_text(
        "Выберите услугу", reply_markup=services_keyboard(services, (await tenant.get_settings()).currency)
    )
    await callback.answer()


@router.callback_query(BookingFSM.select_time, NavCallback.filter(F.target == "back"))
async def back_to_dates(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    data = await state.get_data()
    service_id = data.get("service_id")
    service = await ServiceRepository(session, tenant.master_id).get_by_id(service_id) if service_id is not None else None
    if service is None or not service.active:
        # Услугу удалили/выключили, пока клиент листал шаги: состояние не трогаем, он вернётся «Назад» к списку услуг.
        await callback.answer("Услуга больше недоступна.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    selected = data.get("selected_date")
    await state.set_state(BookingFSM.select_date)
    await _show_booking_calendar(
        callback, session, tenant, tz, service, dt.date.fromisoformat(selected) if selected else dt.datetime.now(tz).date()
    )