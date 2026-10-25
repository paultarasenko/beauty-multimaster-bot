import datetime as dt
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import (
    BOOKINGS_MENU_PROMPT,
    BTN_BOOKINGS_3MONTHS,
    BTN_BOOKINGS_BACK,
    BTN_BOOKINGS_DELETE,
    BTN_BOOKINGS_MONTH,
    BTN_BOOKINGS_WEEK,
    admin_bookings_menu_keyboard,
    admin_menu_keyboard,
    bookings_to_delete_keyboard,
    delete_confirm_keyboard,
)
from app.bot.keyboards.callback_data import AdminBookingCallback, AdminBookingsPageCallback
from app.bot.keyboards.client_keyboards import main_menu_keyboard
from app.bot.menu_mode import enter_admin_panel, leave_admin_panel
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.database.models import Booking, BookingStatus, User
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.user_repository import UserRepository
from app.services.booking_service import booking_service_for
from app.services.exceptions import BookingNotCancellableError, BookingNotFoundError
from app.utils.invite import build_share_url
from app.utils.master_settings import DEFAULT_CURRENCY
from app.utils.money import format_money
from app.utils.timezone import format_time_range, get_master_tz, to_local, weekday_name_ru
from app.tenancy import TenantContext, is_owner

logger = logging.getLogger(__name__)

MAX_DELETE_LIST = 10  # сколько записей на одной странице «Отменить запись»
MAX_MESSAGE_LEN = 4000  # у Telegram лимит 4096 символов на сообщение
WEEKDAYS_RU = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
# Эмодзи-часы по 12-часовому циферблату: индекс = час % 12 (0 -> 12).
CLOCK_FULL = ("🕛", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚")  # :00
CLOCK_HALF = ("🕧", "🕜", "🕝", "🕞", "🕟", "🕠", "🕡", "🕢", "🕣", "🕤", "🕥", "🕦")  # :30


def _clock_emoji(t: dt.datetime) -> str:
    """Часы, показывающие время записи с точностью до получаса (вниз): 15:15 -> 🕒, 18:30 -> 🕡."""
    table = CLOCK_HALF if t.minute >= 30 else CLOCK_FULL
    return table[t.hour % 12]


def _client_display(booking: Booking, user: User) -> str:
    """Кого показать мастеру: реального клиента или того, кого он вписал вручную."""
    if booking.manual_client_name:
        contact = f", {booking.manual_client_contact}" if booking.manual_client_contact else ""
        return f"{booking.manual_client_name}{contact}"

    name = " ".join(part for part in (user.first_name, user.last_name) if part) or "Без имени"
    username = f"@{user.username}" if user.username else "нет username"
    return f"{name} ({username})"


def _client_line(booking: Booking, user: User) -> str:
    """Клиент для списка записей: «Имя · @username» (или контакт, если клиента вписал мастер)."""
    if booking.manual_client_name:
        name = booking.manual_client_name
        contact = booking.manual_client_contact
    else:
        name = " ".join(part for part in (user.first_name, user.last_name) if part) or "Без имени"
        contact = f"@{user.username}" if user.username else "нет username"
    return escape(f"{name} · {contact}" if contact else name)


def _day_label(day: dt.date, today: dt.date) -> str:
    """«Сегодня» / «Завтра» / день недели."""
    if day == today:
        return "Сегодня"
    if day == today + dt.timedelta(days=1):
        return "Завтра"
    return WEEKDAYS_RU[day.weekday()]


def _format_booking_block(
    b: Booking, user: User, tz: dt.tzinfo, today: dt.date, currency: str = DEFAULT_CURRENCY
) -> str:
    """Одна запись в списке мастера:

    📅 06.10 · Вторник · 🕔 17:00 - 19:00
    🤍Французский маникюр · 95 BYN
    👤 Имя · @username
    """
    start_local = to_local(b.start_datetime, tz)
    day = start_local.date()
    return (
        f"📅 {day.strftime('%d.%m')} · {_day_label(day, today)} · "
        f"{_clock_emoji(start_local)} {format_time_range(b.start_datetime, b.end_datetime, tz)}\n"
        f"{escape(b.service_name_snapshot)} · {format_money(b.service_price_snapshot, currency)}\n"
        f"👤 {_client_line(b, user)}"
    )


router = Router(name="admin_menu")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await enter_admin_panel(session, tenant.master_id, message.from_user.id)
    await message.answer("✨ Админ-панель мастера", reply_markup=admin_menu_keyboard())


@router.message(F.text == "👤 Профиль клиента")
async def switch_to_client(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await leave_admin_panel(session, tenant.master_id, message.from_user.id)
    tz = await get_master_tz(session, tenant.master_id)
    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(message.from_user.id)
    has_active_booking = False
    if user is not None:
        upcoming = await BookingRepository(session, tenant.master_id).get_user_upcoming(user.id, dt.datetime.now(tz))
        has_active_booking = len(upcoming) > 0

    await message.answer(
        "Выберите действие", reply_markup=main_menu_keyboard(has_active_booking=has_active_booking, is_admin=True)
    )


# --- Подменю «Записи» ---


@router.message(F.text == "📅 Записи")
async def open_bookings_menu(message: Message) -> None:
    await message.answer(BOOKINGS_MENU_PROMPT, reply_markup=admin_bookings_menu_keyboard())


@router.message(F.text == BTN_BOOKINGS_BACK)
async def back_to_admin_menu(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await enter_admin_panel(session, tenant.master_id, message.from_user.id)
    await message.answer("✨ Админ-панель мастера", reply_markup=admin_menu_keyboard())


# --- На неделю / На месяц ---


async def _answer_bookings_list(message: Message, session: AsyncSession, days: int, empty_text: str, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    now = dt.datetime.now(tz)
    today_end = dt.datetime.combine(now.date(), dt.time.max, tzinfo=tz)
    period_end = today_end + dt.timedelta(days=days)

    result = await session.execute(
        select(Booking, User)
        .join(User, User.id == Booking.user_id)
        .where(
            Booking.master_id == tenant.master_id,
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.start_datetime >= now,
            Booking.start_datetime <= period_end,
        )
        .order_by(Booking.start_datetime)
    )
    rows = result.all()
    if not rows:
        await message.answer(empty_text)
        return

    currency = (await tenant.get_settings()).currency
    blocks = [_format_booking_block(b, user, tz, now.date(), currency) for b, user in rows]

    # На месяц текст может не влезть в одно сообщение — режем по записям, чтобы не рвать запись пополам.
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    for chunk in chunks:
        await message.answer(chunk)


@router.message(F.text == BTN_BOOKINGS_WEEK)
async def show_week_bookings(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await _answer_bookings_list(message, session, days=7, empty_text="На ближайшую неделю записей нет.", tenant=tenant)


@router.message(F.text == BTN_BOOKINGS_MONTH)
async def show_month_bookings(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await _answer_bookings_list(message, session, days=30, empty_text="На ближайший месяц записей нет.", tenant=tenant)


@router.message(F.text == BTN_BOOKINGS_3MONTHS)
async def show_3months_bookings(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await _answer_bookings_list(message, session, days=90, empty_text="На ближайшие три месяца записей нет.", tenant=tenant)


# --- Отменить запись ---


def _delete_button_label(booking: Booking, user: User, tz: dt.tzinfo) -> str:
    start_local = to_local(booking.start_datetime, tz)
    name = booking.manual_client_name or user.first_name or (f"@{user.username}" if user.username else "Без имени")
    return f"🗑 {start_local.strftime('%d.%m %H:%M')} — {booking.service_name_snapshot} — {name}"


async def _build_delete_list(session: AsyncSession, page: int = 0, *, tenant: TenantContext) -> tuple[str, InlineKeyboardMarkup | None]:
    """Страница списка «Отменить запись»: MAX_DELETE_LIST ближайших записей, дальше — кнопкой «Дальше ➡️».

    Если запрошенная страница опустела (записи удалили или отменили), показывается последняя существующая."""
    tz = await get_master_tz(session, tenant.master_id)
    now = dt.datetime.now(tz)
    page = max(page, 0)
    while True:
        result = await session.execute(
            select(Booking, User)
            .join(User, User.id == Booking.user_id)
            .where(
                Booking.master_id == tenant.master_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime >= now,
            )
            .order_by(Booking.start_datetime, Booking.id)
            .offset(page * MAX_DELETE_LIST)
            .limit(MAX_DELETE_LIST + 1)  # одна лишняя запись — признак того, что есть следующая страница
        )
        rows = result.all()
        if rows or page == 0:
            break
        page -= 1
    if not rows:
        return "Предстоящих записей нет.", None

    has_next = len(rows) > MAX_DELETE_LIST
    items = [(b.id, _delete_button_label(b, user, tz)) for b, user in rows[:MAX_DELETE_LIST]]
    return (
        "Выберите запись, которую нужно удалить:",
        bookings_to_delete_keyboard(items, page=page, has_prev=page > 0, has_next=has_next),
    )


def _deletion_text(booking: Booking, tz: dt.tzinfo, currency: str = DEFAULT_CURRENCY) -> str:
    """Текст об отмене записи, который получает клиент."""
    start_local = to_local(booking.start_datetime, tz)
    price_text = format_money(booking.service_price_snapshot, currency)
    if booking.manual_client_name:
        # Запись создал мастер вручную — отдельный текст без призыва записаться заново.
        return (
            "❌ Мастер отменил вашу запись\n\n"
            f"{escape(booking.service_name_snapshot)}\n\n"
            f"📅 {start_local.strftime('%d.%m.%Y')} - {weekday_name_ru(start_local)}\n\n"
            f"🕐 {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n\n"
            f"💰 {price_text}"
        )
    return (
        "❌ Ваша запись отменена мастером\n\n"
        f"{escape(booking.service_name_snapshot)}\n"
        f"📅 {start_local.strftime('%d.%m.%Y')} - {weekday_name_ru(start_local)}\n"
        f"🕐 {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n"
        f"💰 {price_text}\n\n"
        "Чтобы выбрать другое время, нажмите «📅 Записаться»."
    )


async def _resolve_deletion_recipient(session: AsyncSession, booking: Booking, client: User, tenant: TenantContext) -> User | None:
    """Кому можно написать об отмене в Telegram, либо None.

    Запись, вписанную мастером вручную, могли привязать к самому мастеру (клиента в боте не было).
    Но клиент мог успеть зайти в бота уже после создания записи — тогда находим его по @username
    из контакта и пишем ему."""
    if not is_owner(tenant.master, client.telegram_id):
        return client
    contact = (booking.manual_client_contact or "").strip()
    if contact.startswith("@"):
        found = await UserRepository(session, tenant.master_id).get_by_username(contact.lstrip("@"))
        if found is not None and not is_owner(tenant.master, found.telegram_id):
            return found
    return None


async def _share_cancellation_keyboard(
    bot: Bot, booking: Booking, tz: dt.tzinfo
) -> InlineKeyboardMarkup | None:
    """Кнопка «Отправить клиенту»: бот не может первым написать человеку, который его не запускал,
    поэтому мастер пересылает текст об отмене клиенту сам — из своего аккаунта."""
    try:
        bot_username = (await bot.me()).username
    except Exception:
        return None
    if not bot_username:
        return None
    start_local = to_local(booking.start_datetime, tz)
    text = (
        f"❌ Ваша запись отменена: {booking.service_name_snapshot} — "
        f"{start_local.strftime('%d.%m.%Y')} в "
        f"{format_time_range(booking.start_datetime, booking.end_datetime, tz)}. "
        "Чтобы выбрать другое время, откройте бота."
    )
    share_url = build_share_url(f"https://t.me/{bot_username}", text)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📨 Отправить клиенту", url=share_url)]]
    )


async def _notify_client_about_deletion(
    bot: Bot, session: AsyncSession, booking: Booking, tz: dt.tzinfo, tenant: TenantContext
) -> tuple[bool, str]:
    """Отправляет клиенту сообщение об отмене. Возвращает (доставлено ли, пометка для мастера)."""
    client = await UserRepository(session, tenant.master_id).get_by_id(booking.user_id)
    if client is None:
        return False, "⚠️ Клиент не найден — уведомление не отправлено."

    recipient = await _resolve_deletion_recipient(session, booking, client, tenant=tenant)
    if recipient is None:
        return False, "Клиента ещё нет в боте — написать ему в Telegram автоматически нельзя."

    try:
        await bot.send_message(
            recipient.telegram_id, _deletion_text(booking, tz, (await tenant.get_settings()).currency)
        )
    except TelegramAPIError:
        logger.warning("Не удалось уведомить клиента об удалении записи %s", booking.id, exc_info=True)
        return False, "⚠️ Не удалось отправить уведомление клиенту (возможно, он заблокировал бота)."
    return True, "Клиент уведомлён."


@router.message(F.text == BTN_BOOKINGS_DELETE)
async def open_delete_list(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    text, keyboard = await _build_delete_list(session, tenant=tenant)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(AdminBookingCallback.filter(F.action == "delete"))
async def ask_delete_confirmation(
    callback: CallbackQuery, callback_data: AdminBookingCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    booking = await BookingRepository(session, tenant.master_id).get_by_id(callback_data.booking_id)
    if booking is None or booking.status != BookingStatus.CONFIRMED.value:
        await callback.answer("Эта запись уже недоступна.", show_alert=True)
        return

    user = await UserRepository(session, tenant.master_id).get_by_id(booking.user_id)
    tz = await get_master_tz(session, tenant.master_id)
    start_local = to_local(booking.start_datetime, tz)
    price_text = format_money(booking.service_price_snapshot, (await tenant.get_settings()).currency)
    text = (
        "Удалить эту запись?\n\n"
        f"👤 {escape(_client_display(booking, user))}\n"
        f"{escape(booking.service_name_snapshot)}\n"
        f"📅 {start_local.strftime('%d.%m.%Y')}\n"
        f"🕐 {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n"
        f"💰 {price_text}"
    )
    await callback.message.edit_text(text, reply_markup=delete_confirm_keyboard(booking.id, page=callback_data.page))
    await callback.answer()


async def _show_delete_list_page(callback: CallbackQuery, session: AsyncSession, page: int, tenant: TenantContext) -> None:
    text, keyboard = await _build_delete_list(session, page, tenant=tenant)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        # Двойное нажатие на «Дальше»: сообщение уже показывает эту страницу — это не ошибка.
        if "message is not modified" not in str(exc):
            raise
    await callback.answer()


@router.callback_query(AdminBookingsPageCallback.filter())
async def change_delete_list_page(
    callback: CallbackQuery, callback_data: AdminBookingsPageCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    await _show_delete_list_page(callback, session, callback_data.page, tenant=tenant)


@router.callback_query(AdminBookingCallback.filter(F.action == "back"))
async def back_to_delete_list(callback: CallbackQuery, callback_data: AdminBookingCallback, session: AsyncSession, tenant: TenantContext) -> None:
    # «Назад» с экрана подтверждения возвращает на ту страницу списка, с которой запись была открыта.
    await _show_delete_list_page(callback, session, callback_data.page, tenant=tenant)


@router.callback_query(AdminBookingCallback.filter(F.action == "confirm"))
async def confirm_delete(callback: CallbackQuery, callback_data: AdminBookingCallback, session: AsyncSession, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    booking_service = await booking_service_for(session, tz, tenant)
    try:
        booking = await booking_service.cancel_booking(callback_data.booking_id)
        await session.commit()
    except (BookingNotFoundError, BookingNotCancellableError):
        await session.rollback()
        await callback.answer("Эту запись уже нельзя удалить.", show_alert=True)
        return

    delivered, note = await _notify_client_about_deletion(callback.bot, session, booking, tz, tenant=tenant)
    keyboard = None
    if delivered:
        result_text = "✅ Запись отменена, клиент уведомлён."
    else:
        result_text = f"✅ Запись отменена.\n\n{note}"
        if booking.manual_client_name:
            contact = f" ({escape(booking.manual_client_contact)})" if booking.manual_client_contact else ""
            result_text += (
                f"\n\n👤 {escape(booking.manual_client_name)}{contact}\n"
                "Нажмите кнопку ниже и перешлите клиенту сообщение об отмене."
            )
            keyboard = await _share_cancellation_keyboard(callback.bot, booking, tz)
    await callback.message.edit_text(result_text, reply_markup=keyboard)
    await callback.answer()