import asyncio
import datetime as dt
import logging
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.keyboards.reminder_keyboards import reminder_keyboard
from app.config.settings import settings
from app.database.models import Booking, User
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.user_repository import UserRepository
from app.scheduler.throttle import RateLimiter
from app.services.master_settings_service import load_master_settings
from app.services.quiet_mode_service import defer_if_quiet
from app.services.message_texts import booking_template_values
from app.utils.text_templates import render_template
from app.utils.reminder_texts import (
    client_reminder_2h_text,
    client_reminder_24h_text,
    master_reminder_2h_text,
)
from app.tenancy import get_owner_telegram_id
from app.utils.timezone import format_time_range, get_master_tz

logger = logging.getLogger(__name__)


async def _deliver(bot: Bot, limiter: RateLimiter | None, chat_id: int, text: str, **kwargs) -> None:
    """Отправка в Telegram: очередь ограничителя скорости ЭТОГО бота (если передан), затем сама отправка с таймаутом.

    Таймаут не включает ожидание очереди ограничителя. Ошибки не перехватываются: их классифицирует вызывающий
    (app.services.delivery).
    """
    if limiter is not None:
        await limiter.acquire()
    await asyncio.wait_for(bot.send_message(chat_id, text, **kwargs), timeout=settings.scheduler_send_timeout_seconds)


def is_unclaimed_manual_booking(booking: Booking, owner: User, owner_telegram_id: int | None) -> bool:
    """Запись, которую мастер создал вручную для человека, которого ещё нет в боте.

    Пока клиент не открыл ссылку-приглашение, запись числится за самим мастером, а настоящие данные клиента лежат
    в manual_client_*. Напоминания «клиенту» по такой записи нельзя слать владельцу: они пришли бы мастеру.
    """
    return (
        booking.manual_client_name is not None
        and owner_telegram_id is not None
        and owner.telegram_id == owner_telegram_id
    )


def make_send_reminder_func(
    bot: Bot, session_factory: async_sessionmaker, master_id: int, *, limiter: RateLimiter | None = None
):
    async def send_reminder(booking: Booking) -> None:
        async with session_factory() as session:
            user = await UserRepository(session, master_id).get_by_id(booking.user_id)
            profile = await ContentRepository(session, master_id).get_master_profile()
            address = profile.address if profile else None
            master_name = (profile.name if profile else "") or ""
            tz = await get_master_tz(session, master_id)
            owner_telegram_id = await get_owner_telegram_id(session, master_id)
            master_settings = await load_master_settings(session, master_id)  # настройки на момент отправки

        if is_unclaimed_manual_booking(booking, user, owner_telegram_id):
            logger.info("Напоминание за сутки пропущено: клиент по записи %s ещё не открыл приглашение", booking.id)
            return

        values = booking_template_values(
            booking, tz, client_name=user.first_name, address=address or "", master_name=master_name,
            currency=master_settings.currency,
        )
        text = render_template(
            master_settings.reminder_24h_text,
            values,
            lambda: client_reminder_24h_text(
                booking.service_name_snapshot,
                format_time_range(booking.start_datetime, booking.end_datetime, tz),
                booking.service_price_snapshot,
                address,
                currency=master_settings.currency,
            ),
            context="reminder_24h",
        )

        await _deliver(
            bot,
            limiter,
            user.telegram_id,
            text,
            reply_markup=reminder_keyboard(booking.id, master_settings.allow_reschedule),
        )

    return send_reminder


def make_send_client_reminder_2h_func(
    bot: Bot, session_factory: async_sessionmaker, master_id: int, *, limiter: RateLimiter | None = None
):
    """Напоминание клиенту за 2 часа: «Через 2 часа у вас запись к Анне: Маникюр, 15:00»."""

    async def send_client_reminder_2h(booking: Booking) -> None:
        if booking.start_datetime <= dt.datetime.now(dt.timezone.utc):
            return  # воркер простоял и опоздал — «через 2 часа» уже неправда
        async with session_factory() as session:
            user = await UserRepository(session, master_id).get_by_id(booking.user_id)
            profile = await ContentRepository(session, master_id).get_master_profile()
            master_name = (profile.name if profile else "") or ""  # имя могли удалить
            address = (profile.address if profile else "") or ""
            tz = await get_master_tz(session, master_id)
            owner_telegram_id = await get_owner_telegram_id(session, master_id)
            master_settings = await load_master_settings(session, master_id)

        if is_unclaimed_manual_booking(booking, user, owner_telegram_id):
            logger.info("Напоминание за 2 часа пропущено: клиент по записи %s ещё не открыл приглашение", booking.id)
            return

        values = booking_template_values(
            booking, tz, client_name=user.first_name, address=address, master_name=master_name,
            currency=master_settings.currency,
        )
        text = render_template(
            master_settings.reminder_2h_text,
            values,
            lambda: client_reminder_2h_text(
                master_name,
                booking.service_name_snapshot,
                format_time_range(booking.start_datetime, booking.end_datetime, tz),
                booking.service_price_snapshot,
                address,
                currency=master_settings.currency,
            ),
            context="reminder_2h",
        )
        await _deliver(bot, limiter, user.telegram_id, text)

    return send_client_reminder_2h


def make_send_master_reminder_2h_func(
    bot: Bot, session_factory: async_sessionmaker, master_id: int, *, limiter: RateLimiter | None = None
):
    """Напоминание мастеру за 2 часа: имя клиента, услуга, время, @username, длительность, цена."""

    async def send_master_reminder_2h(booking: Booking) -> None:
        if booking.start_datetime <= dt.datetime.now(dt.timezone.utc):
            return
        async with session_factory() as session:
            owner_telegram_id = await get_owner_telegram_id(session, master_id)
            if not owner_telegram_id:
                logger.warning("Напоминание мастеру не отправлено: у мастера id=%s не задан owner_telegram_id", master_id)
                return
            user = await UserRepository(session, master_id).get_by_id(booking.user_id)
            tz = await get_master_tz(session, master_id)
            master_settings = await load_master_settings(session, master_id)

        unclaimed = is_unclaimed_manual_booking(booking, user, owner_telegram_id)
        text = master_reminder_2h_text(
            client_first_name=user.first_name,
            client_username=user.username,
            service_name=booking.service_name_snapshot,
            time_str=format_time_range(booking.start_datetime, booking.end_datetime, tz),
            duration_minutes=booking.service_duration_snapshot,
            price=booking.service_price_snapshot,
            manual_client_name=booking.manual_client_name if unclaimed else None,
            manual_client_contact=booking.manual_client_contact if unclaimed else None,
            currency=master_settings.currency,
        )
        # Тихий режим мастера: напоминание откладывается и уходит после его окончания, но только если запись ещё не началась.
        async with session_factory() as session:
            if await defer_if_quiet(session, master_id, text, expires_at=booking.start_datetime):
                await session.commit()
                return
        await _deliver(bot, limiter, owner_telegram_id, text)

    return send_master_reminder_2h


def make_send_admin_reminder_func(
    bot: Bot,
    owner_telegram_id: int,
    *,
    limiter: RateLimiter | None = None,
    session_factory: async_sessionmaker | None = None,
    master_id: int | None = None,
):
    """Личное напоминание мастера самому себе (владельцу мастера): текст, который он сам задал.

    Если переданы `session_factory` и `master_id`, учитывается тихий режим мастера: напоминание откладывается и уходит после его окончания."""

    async def send_admin_reminder(text: str) -> None:
        full_text = f"🔔 <b>Напоминание</b>\n\n{escape(text)}"
        if session_factory is not None and master_id is not None:
            async with session_factory() as session:
                if await defer_if_quiet(session, master_id, full_text):
                    await session.commit()
                    return
        await _deliver(bot, limiter, owner_telegram_id, full_text)

    return send_admin_reminder


def make_send_deferred_func(bot: Bot, owner_telegram_id: int, *, limiter: RateLimiter | None = None):
    """Отправка мастеру сообщения, отложенного тихим режимом (текст и клавиатура сохранены как были)."""

    async def send_deferred(text: str, reply_markup: InlineKeyboardMarkup | None) -> None:
        if reply_markup is None:
            await _deliver(bot, limiter, owner_telegram_id, text)
        else:
            await _deliver(bot, limiter, owner_telegram_id, text, reply_markup=reply_markup)

    return send_deferred


def start_reminder_scheduler(bot: Bot, session_factory: async_sessionmaker, master_id: int) -> AsyncIOScheduler:
    """Режим polling: единый планировщик (app/scheduler/service.py) с одним мастером и одним ботом.

    Публичное поведение прежнее: возвращает запущенный AsyncIOScheduler с задачами `reminder_worker`,
    `admin_reminder_worker`, `booking_completion_worker`; источник истины — БД, планировщик лишь тикает. Для штатной
    остановки с ожиданием текущих отправок используйте `SchedulerService` напрямую (так делает app/main.py).
    """
    from app.scheduler.service import SchedulerService
    from app.scheduler.tenants import SingleTenantSource

    return SchedulerService(session_factory, SingleTenantSource(session_factory, bot, master_id)).start()
