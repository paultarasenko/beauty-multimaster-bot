import datetime as dt
import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import admin_menu_keyboard
from app.bot.keyboards.client_keyboards import main_menu_keyboard, my_booking_keyboard
from app.bot.menu_mode import enter_admin_panel, leave_admin_panel
from app.bot.middlewares.admin_filter import IsAdminFilter, get_admin_contact_username
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.user_repository import UserRepository
from app.services.booking_service import booking_service_for
from app.services.notifier import MasterNotifier
from app.utils.greeting import DEFAULT_GREETING
from app.utils.invite import INVITE_START_PREFIX
from app.utils.master_settings import client_reminder_phrase
from app.utils.money import format_money
from app.utils.timezone import format_time_range, get_master_tz, to_local, weekday_name_ru
from app.tenancy import TenantContext, is_owner

logger = logging.getLogger(__name__)

router = Router(name="client_start")


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(INVITE_START_PREFIX)))
async def cmd_start_invite(message: Message, command: CommandObject, session: AsyncSession, tenant: TenantContext) -> None:
    """/start по ссылке-приглашению от мастера: привязывает к клиенту запись, которую мастер создал
    для него вручную, и сообщает об этом и клиенту, и мастеру. Регистрируется раньше cmd_start,
    чтобы обычный /start (без приглашения) по-прежнему попадал в него."""
    if is_owner(tenant.master, message.from_user.id):
        # Мастер сам открыл ссылку (например, проверяет её) — привязывать запись не к кому.
        await cmd_start(message, session, tenant=tenant)
        return

    token = (command.args or "")[len(INVITE_START_PREFIX):]
    user = await UserRepository(session, tenant.master_id).get_or_create(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    tz = await get_master_tz(session, tenant.master_id)
    booking = await (await booking_service_for(session, tz, tenant)).claim_invitation(token, user.id) if token else None
    await session.commit()

    # Приветствие и меню — после привязки, чтобы в меню уже была кнопка записи клиента.
    await cmd_start(message, session, tenant=tenant)

    if booking is None:
        await message.answer(
            "Эта ссылка-приглашение недействительна: она уже использована, а запись отменена или прошла. "
            "Если вам нужна запись, выберите время в меню."
        )
        return

    start_local = to_local(booking.start_datetime, tz)
    master_settings = await tenant.get_settings()
    reminder_phrase = client_reminder_phrase(master_settings)
    contact_username = await get_admin_contact_username(message.bot, tenant.master)
    await message.answer(
        "📝 Вы записаны\n\n"
        f"{escape(booking.service_name_snapshot)}\n"
        f"📅 {start_local.strftime('%d.%m.%Y')} - {weekday_name_ru(start_local)}\n"
        f"🕐 {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n"
        f"💰 {format_money(booking.service_price_snapshot, master_settings.currency)}"
        + (f"\n\n🔔 Вы получите уведомление {reminder_phrase} 🙂" if reminder_phrase else ""),
        reply_markup=my_booking_keyboard(booking.id, contact_username, master_settings.allow_reschedule),
    )

    if tenant.master.owner_telegram_id:
        who = booking.manual_client_name or user.first_name or "Клиент"
        handle = f" (@{user.username})" if user.username else ""
        try:
            await message.bot.send_message(
                tenant.master.owner_telegram_id,
                f"✅ {escape(who)}{escape(handle)} открыл приглашение: запись на "
                f"{start_local.strftime('%d.%m.%Y')} в {format_time_range(booking.start_datetime, booking.end_datetime, tz)} теперь привязана к нему, "
                "напоминания будут приходить ему.",
            )
        except Exception:
            logger.exception("Не удалось уведомить мастера о принятом приглашении (booking_id=%s)", booking.id)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    user_repo = UserRepository(session, tenant.master_id)
    # Проверка строго ДО leave_admin_panel: он создаёт строку в users (set_admin_panel_mode),
    # и после него любой пользователь уже «существует» — новый от старого не отличить.
    is_new_user = (
        not is_owner(tenant.master, message.from_user.id)
        and await user_repo.get_by_telegram_id(message.from_user.id) is None
    )
    await leave_admin_panel(session, tenant.master_id, message.from_user.id)
    user = await user_repo.get_or_create(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    tz = await get_master_tz(session, tenant.master_id)
    booking_repo = BookingRepository(session, tenant.master_id)
    upcoming = await booking_repo.get_user_upcoming(user.id, dt.datetime.now(tz))

    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    greeting = profile.greeting_text if profile is not None and profile.greeting_text else DEFAULT_GREETING

    await message.answer(
        escape(greeting),
        reply_markup=main_menu_keyboard(
            has_active_booking=len(upcoming) > 0, is_admin=is_owner(tenant.master, message.from_user.id)
        ),
    )

    if is_new_user:
        try:
            await MasterNotifier(message.bot, master_id=tenant.master_id, owner_telegram_id=tenant.master.owner_telegram_id, session=tenant.session).notify_new_user(user)
        except Exception:
            logger.exception("Не удалось уведомить мастера о новом пользователе (telegram_id=%s)", user.telegram_id)


@router.message(F.text == "👑 Профиль мастера", IsAdminFilter())
async def switch_to_admin(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await enter_admin_panel(session, tenant.master_id, message.from_user.id)
    await message.answer("✨ Админ-панель мастера", reply_markup=admin_menu_keyboard())


@router.message(F.text == "🏠 Главное меню")
async def show_main_menu(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await leave_admin_panel(session, tenant.master_id, message.from_user.id)
    user_repo = UserRepository(session, tenant.master_id)
    user = await user_repo.get_by_telegram_id(message.from_user.id)

    has_active_booking = False
    if user is not None:
        tz = await get_master_tz(session, tenant.master_id)
        booking_repo = BookingRepository(session, tenant.master_id)
        upcoming = await booking_repo.get_user_upcoming(user.id, dt.datetime.now(tz))
        has_active_booking = len(upcoming) > 0

    await message.answer(
        "Выберите действие",
        reply_markup=main_menu_keyboard(
            has_active_booking=has_active_booking, is_admin=is_owner(tenant.master, message.from_user.id)
        ),
    )