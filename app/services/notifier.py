import datetime as dt
import logging
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Booking, User
from app.services.quiet_mode_service import defer_if_quiet
from app.utils.master_settings import DEFAULT_CURRENCY, count_genitive
from app.utils.money import format_money
from app.utils.timezone import format_time_range, to_local

logger = logging.getLogger(__name__)


CLIENT_RESTRICTED_NO_BOOKINGS_TEXT = (
    "🚫 Мастер ограничил вам доступ: новые записи недоступны.\n\n"
    "Вы можете связаться с мастером, перейдя в раздел информация и отправить сообщение."
)

_COUNT_WORDS = {3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь", 8: "восемь", 9: "девять", 10: "десять"}


def _count(n: int) -> str:
    return _COUNT_WORDS.get(n, str(n))


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def cancellation_streak_headline(streak: int) -> str:
    """«Клиент записался три раза подряд и создал три отмены!» — с реальной длиной серии (5 → «пять раз … пять отмен»)."""
    times = _plural(streak, "раз", "раза", "раз")
    cancels = _plural(streak, "отмену", "отмены", "отмен")
    return f"⚠️ Клиент записался {_count(streak)} {times} подряд и создал {_count(streak)} {cancels}!"


def over_limit_cancellation_headline(cancelled: int, limit: int = 3) -> str:
    """Клиент с записью сверх лимита отменил несколько записей (не обязательно подряд). `limit` — лимит мастера."""
    records = _plural(cancelled, "запись", "записи", "записей")
    return f"⚠️ Клиент отменил {_count(cancelled)} {records}, хотя у него было больше {count_genitive(limit)} записей!"


def _bookings_lines(bookings: list[Booking], tz: dt.tzinfo) -> str:
    return "\n\n".join(
        f"{escape(b.service_name_snapshot)}\n"
        f"📅 {to_local(b.start_datetime, tz).strftime('%d.%m.%Y')}  "
        f"🕐 {format_time_range(b.start_datetime, b.end_datetime, tz)}"
        for b in bookings
    )


def _cancelled_bookings_for_client(bookings: list[Booking], tz: dt.tzinfo, currency: str = DEFAULT_CURRENCY) -> str:
    """Отменённые записи для сообщения клиенту: услуга, дата и время отдельными строками, ниже — стоимость."""
    return "\n\n".join(
        f"{escape(b.service_name_snapshot)}\n"
        f"📅 {to_local(b.start_datetime, tz).strftime('%d.%m.%Y')}\n"
        f"🕐 {format_time_range(b.start_datetime, b.end_datetime, tz)}\n\n"
        f"💰 {format_money(b.service_price_snapshot, currency)}"
        for b in bookings
    )


def _client_block(client: User) -> str:
    """Строки «Клиент / Telegram» для сообщений мастеру. Имя и username — пользовательский ввод, а бот шлёт HTML."""
    client_line = f"@{escape(client.username)}" if client.username else "нет username"
    return f"👤 Клиент: {escape(client.first_name or '')}\n\n📱 Telegram: {client_line}\n\n"


class MasterNotifier:
    """Уведомления мастеру. `master_id` — чьему мастеру они адресованы, `owner_telegram_id` — его владелец
    (masters.owner_telegram_id): сюда уходят все сообщения «мастеру». Хендлеры передают его из `tenant.master`
    (мастер загружен на этот же апдейт, значит значение свежее). Владельца нет (None) — ничего не отправляется,
    в лог пишется предупреждение.

    `session` — сессия БД апдейта: по ней проверяется тихий режим мастера. Пока он действует, сообщение мастеру
    сохраняется в БД и уходит после его окончания (см. `app/services/quiet_mode_service.py`); без сессии тихий режим
    не проверяется. Сообщения клиентам тихий режим не затрагивает."""

    def __init__(
        self, bot: Bot, *, master_id: int, owner_telegram_id: int | None, session: AsyncSession | None = None
    ):
        self.bot = bot
        self.master_id = master_id
        self.owner_telegram_id = owner_telegram_id
        self.session = session

    def _has_recipient(self) -> bool:
        if self.owner_telegram_id:
            return True
        logger.warning("Уведомление мастеру не отправлено: у мастера id=%s не задан owner_telegram_id", self.master_id)
        return False

    async def _defer_if_quiet(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
        """True — у мастера тихий режим: сообщение сохранено и уйдёт после его окончания (отправлять сейчас не нужно).

        Любой сбой при проверке или сохранении — не причина терять уведомление: возвращаем False, и сообщение уходит сразу.
        Коммит нужен, чтобы отложенное сообщение не пропало, если позже в этом же апдейте что-то упадёт (к моменту
        уведомления данные, о которых оно, уже сохранены хендлером)."""
        if self.session is None:
            return False
        try:
            if not await defer_if_quiet(self.session, self.master_id, text, reply_markup):
                return False
            await self.session.commit()
            return True
        except Exception:
            logger.exception("Не удалось отложить сообщение мастеру в тихом режиме (master_id=%s)", self.master_id)
            await self.session.rollback()
            return False

    async def notify_new_booking(
        self, booking: Booking, client: User, tz: dt.tzinfo, currency: str = DEFAULT_CURRENCY
    ) -> None:
        start_local = to_local(booking.start_datetime, tz)
        text = (
            "🔔 Новая запись\n\n"
            f"{_client_block(client)}"
            f"{escape(booking.service_name_snapshot)}\n\n"
            f"📅 {start_local.strftime('%d.%m.%Y')}\n\n"
            f"🕐 {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n\n"
            f"💰 {format_money(booking.service_price_snapshot, currency)}"
        )
        await self._send(text)

    async def notify_cancelled_booking(
        self, booking: Booking, client: User, tz: dt.tzinfo, currency: str = DEFAULT_CURRENCY
    ) -> None:
        start_local = to_local(booking.start_datetime, tz)
        text = (
            "❌ Запись отменена\n\n"
            f"{_client_block(client)}"
            f"{escape(booking.service_name_snapshot)}\n\n"
            f"📅 {start_local.strftime('%d.%m.%Y')}\n\n"
            f"🕐 {format_time_range(booking.start_datetime, booking.end_datetime, tz)}\n\n"
            f"💰 {format_money(booking.service_price_snapshot, currency)}"
        )
        await self._send(text)

    async def notify_rescheduled_booking(
        self, old_booking: Booking, new_booking: Booking, client: User, tz: dt.tzinfo, currency: str = DEFAULT_CURRENCY
    ) -> None:
        old_local = to_local(old_booking.start_datetime, tz)
        new_local = to_local(new_booking.start_datetime, tz)
        text = (
            "🔄 Запись перенесена\n\n"
            f"{_client_block(client)}"
            f"{escape(new_booking.service_name_snapshot)}\n\n"
            "Было:\n"
            f"📅 {old_local.strftime('%d.%m.%Y')}\n\n"
            f"🕐 {format_time_range(old_booking.start_datetime, old_booking.end_datetime, tz)}\n\n"
            "Стало:\n"
            f"📅 {new_local.strftime('%d.%m.%Y')}\n\n"
            f"🕐 {format_time_range(new_booking.start_datetime, new_booking.end_datetime, tz)}\n\n"
            f"💰 {format_money(new_booking.service_price_snapshot, currency)}"
        )
        await self._send(text)

    async def notify_new_user(self, user: User) -> None:
        """Человек впервые нажал /start и появился в боте."""
        full_name = " ".join(part for part in (user.first_name, user.last_name) if part) or "без имени"
        username_line = f"@{escape(user.username)}" if user.username else "нет username"
        text = (
            "👋 Новый пользователь в боте\n\n"
            f"👤 {escape(full_name)}\n\n"
            f"📱 Telegram: {username_line}\n\n"
            f"🆔 ID: {user.telegram_id}"
        )
        await self._send(text)

    async def notify_client_blocked_bot(self, user) -> None:
        """Клиент заблокировал бота: напоминания и сообщения ему больше не дойдут."""
        full_name = " ".join(part for part in (user.first_name, user.last_name) if part) or "без имени"
        username_line = f"@{escape(user.username)}" if user.username else "нет username"
        text = (
            "🚫 Клиент заблокировал бота\n\n"
            f"👤 {escape(full_name)}\n\n"
            f"📱 Telegram: {username_line}\n\n"
            f"🆔 ID: {user.id}"
        )
        await self._send(text)

    async def notify_cancellation_streak(
        self,
        client: User,
        completed_count: int,
        streak: int = 3,
        over_limit: bool = False,
        max_active_bookings: int = 3,
    ) -> None:
        """Клиент записался и отменил несколько записей подряд (от трёх): мастер решает, оставить ли ему доступ."""
        from app.bot.keyboards.admin_keyboards import client_access_keyboard

        full_name = " ".join(part for part in (client.first_name, client.last_name) if part) or "без имени"
        username_line = f"@{escape(client.username)}" if client.username else "нет username"
        text = (
            f"{over_limit_cancellation_headline(streak, max_active_bookings) if over_limit else cancellation_streak_headline(streak)}\n\n"
            f"👤 Имя: {escape(full_name)}\n\n"
            f"📱 Telegram: {username_line}\n\n"
            f"✅ Выполненных записей: {completed_count}"
        )
        await self._send(text, reply_markup=client_access_keyboard(client.id))

    async def notify_client_restricted(
        self, client: User, cancelled: list[Booking], tz: dt.tzinfo, currency: str = DEFAULT_CURRENCY
    ) -> None:
        """Клиенту: мастер ограничил доступ, действующие записи (если были) отменены. Best-effort, как и остальное."""
        if cancelled:
            text = (
                "🚫 Мастер ограничил вам доступ к записи, ваши записи отменены:\n\n"
                f"{_cancelled_bookings_for_client(cancelled, tz, currency)}\n\n"
                "💬 Если у вас есть вопросы, вы можете связаться с мастером: "
                "откройте раздел «ℹ️ Информация» и нажмите «📞 Связаться со мной»."
            )
        else:
            text = CLIENT_RESTRICTED_NO_BOOKINGS_TEXT
        try:
            await self.bot.send_message(client.telegram_id, text)
        except Exception:
            logger.exception("Не удалось уведомить клиента об ограничении доступа (telegram_id=%s)", client.telegram_id)

    async def notify_master_client_restricted(self, client: User, cancelled: list[Booking], tz: dt.tzinfo) -> None:
        """Мастеру: итог после нажатия «Ограничить доступ» — кому закрыт доступ и какие записи отменены."""
        if cancelled:
            result = f"❌ Отменено записей: {len(cancelled)}\n\n{_bookings_lines(cancelled, tz)}"
        else:
            result = "Действующих записей у клиента не было."
        text = f"🚫 Доступ клиента ограничен\n\n{_client_block(client)}{result}\n\nКлиент получил уведомление."
        await self._send(text, respect_quiet=False)  # ответ на нажатие самого мастера — тихий режим его не откладывает

    async def notify_limit_request(self, request, client: User) -> bool:
        """Клиент с максимумом записей просит разрешение на ещё одну: мастер принимает или отклоняет.

        Возвращает False, если доставить мастеру не удалось — тогда запрос не должен «висеть» у клиента."""
        from app.bot.keyboards.admin_keyboards import limit_request_keyboard

        if not self._has_recipient():
            return False
        full_name = " ".join(part for part in (client.first_name, client.last_name) if part) or "без имени"
        username_line = f"@{escape(client.username)}" if client.username else "нет username"
        text = (
            "✉️ Запрос на запись сверх лимита\n\n"
            f"👤 Клиент: {escape(full_name)}\n\n"
            f"📱 Telegram: {username_line}\n\n"
            f"💬 Сообщение:\n{escape(request.message)}"
        )
        markup = limit_request_keyboard(request.id)
        if await self._defer_if_quiet(text, markup):
            return True  # запрос дойдёт до мастера после тихого режима
        try:
            await self.bot.send_message(self.owner_telegram_id, text, reply_markup=markup)
        except Exception:
            logger.exception("Не удалось отправить запрос клиента мастеру (master_id=%s, owner_telegram_id=%s)", self.master_id, self.owner_telegram_id)
            return False
        return True

    async def notify_access_request(self, request, client: User) -> bool:
        """Ограниченный клиент написал мастеру: мастер открывает доступ или ограничивает навсегда.

        False — доставить не удалось; тогда обращение не должно «висеть» у клиента."""
        from app.bot.keyboards.admin_keyboards import access_request_keyboard

        if not self._has_recipient():
            return False
        full_name = " ".join(part for part in (client.first_name, client.last_name) if part) or "без имени"
        username_line = f"@{escape(client.username)}" if client.username else "нет username"
        text = (
            "✉️ Сообщение от клиента с ограниченным доступом\n\n"
            f"👤 Клиент: {escape(full_name)}\n\n"
            f"📱 Telegram: {username_line}\n\n"
            f"💬 Сообщение:\n{escape(request.message)}"
        )
        markup = access_request_keyboard(request.id)
        if await self._defer_if_quiet(text, markup):
            return True  # обращение дойдёт до мастера после тихого режима
        try:
            await self.bot.send_message(self.owner_telegram_id, text, reply_markup=markup)
        except Exception:
            logger.exception("Не удалось отправить обращение клиента мастеру (master_id=%s, owner_telegram_id=%s)", self.master_id, self.owner_telegram_id)
            return False
        return True

    async def _send(
        self, text: str, reply_markup: InlineKeyboardMarkup | None = None, *, respect_quiet: bool = True
    ) -> None:
        """Уведомление мастеру — best-effort: к этому моменту запись уже сохранена и клиенту об этом сказано,
        поэтому сбой доставки (мастер заблокировал бота, сеть) не должен превращаться в «что-то пошло не так»."""
        if not self._has_recipient():
            return
        if respect_quiet and await self._defer_if_quiet(text, reply_markup):
            return
        try:
            if reply_markup is None:
                await self.bot.send_message(self.owner_telegram_id, text)
            else:
                await self.bot.send_message(self.owner_telegram_id, text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Не удалось отправить уведомление мастеру (master_id=%s, owner_telegram_id=%s)", self.master_id, self.owner_telegram_id)