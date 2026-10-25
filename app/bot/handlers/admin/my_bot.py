"""«🤖 Мой бот» мастера (этап 9): название и @username бота, ссылка, статус, проверка подключения и перерегистрация webhook.

Только просмотр и проверка: токен менять, бота отключать и удалять может лишь владелец сервиса (суперадмин-бот / CLI).
Экран открывает только владелец мастера (IsAdminFilter на роутере). Токен и секреты не показываются.
О проблемах, найденных проверкой, суперадмин получает один сигнал (повтор не чаще заданного интервала).
"""

import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards.admin_keyboards import BTN_MY_BOT
from app.bot.keyboards.callback_data import AdminSettingsCallback, MyBotCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.config.settings import settings
from app.multibot import management
from app.multibot.crypto import ConfigurationError, TokenCipher, TokenDecryptionError
from app.services.my_bot_service import MyBotInfo, get_my_bot
from app.superadmin import alerts as alert_texts
from app.superadmin.alerts import AlertNotifier
from app.tenancy import TenantContext

logger = logging.getLogger(__name__)

router = Router(name="admin_my_bot")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

NO_BOT_TEXT = (
    "🤖 Мой бот\n\nПодключённого бота в базе нет: бот работает в служебном режиме. "
    "Подключить бота мастеру может владелец сервиса."
)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
MANAGEMENT_ERRORS = (management.ManagementError, ConfigurationError, TokenDecryptionError)


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить подключение", callback_data=MyBotCallback(action="check").pack())],
            [InlineKeyboardButton(text="🔄 Перерегистрировать webhook", callback_data=MyBotCallback(action="webhook").pack())],
            [InlineKeyboardButton(text="⬅️ Настройки", callback_data=AdminSettingsCallback(action="menu", field="main", value="none").pack())],
        ]
    )


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Мой бот", callback_data=MyBotCallback(action="show").pack())]]
    )


async def _bot_title(message: Message, info: MyBotInfo) -> str:
    """Название бота — у Telegram (в базе хранится только username). Бот апдейта — это и есть бот мастера."""
    try:
        me = await message.bot.me()  # кэшируется в объекте Bot
        return me.full_name
    except Exception:
        return info.bot_username or f"id {info.telegram_bot_id}"


def _card_text(info: MyBotInfo, title: str) -> str:
    username = f"@{escape(info.bot_username)}" if info.bot_username else "—"
    link = f"https://t.me/{escape(info.bot_username)}" if info.bot_username else "—"
    return (
        "🤖 Мой бот\n\n"
        f"Название: {escape(title)}\n"
        f"Username: {username}\n"
        f"Ссылка: {link}\n"
        f"Статус: {'🟢 подключён' if info.is_active else '🔴 отключён владельцем сервиса'}\n\n"
        "Токен, отключение и удаление бота — у владельца сервиса. Здесь можно проверить подключение "
        "и заново зарегистрировать webhook, если бот перестал отвечать."
    )


async def _edit_or_answer(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            await message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


async def _card(message: Message, session: AsyncSession, tenant: TenantContext) -> tuple[str, InlineKeyboardMarkup | None]:
    info = await get_my_bot(session, tenant.master_id)
    if info is None:
        return NO_BOT_TEXT, None
    return _card_text(info, await _bot_title(message, info)), _kb()


async def _notify_superadmin(alerts: AlertNotifier | None, check: management.BotCheck) -> None:
    """Проблема, найденная мастером при проверке, — сигнал суперадмину (дубли отсекает AlertNotifier)."""
    if alerts is None:
        return
    bot_id, master_id, name = check.telegram_bot_id, check.master_id, check.bot_username
    if check.token_valid is False:
        await alerts.notify(alert_texts.KIND_INVALID_TOKEN, bot_id, alert_texts.invalid_token_text(master_id, bot_id, name))
    elif check.error is not None:
        await alerts.notify(
            alert_texts.KIND_CONNECTION_ERROR, bot_id, alert_texts.connection_error_text(master_id, bot_id, name, check.error)
        )
    elif check.webhook_ok is False:
        reason = "У Telegram не установлен webhook." if not check.webhook_registered else "Webhook установлен на другой адрес."
        await alerts.notify(alert_texts.KIND_WEBHOOK_DOWN, bot_id, alert_texts.webhook_down_text(master_id, bot_id, name, reason))


def _check_text(check: management.BotCheck) -> str:
    lines = ["🔍 Проверка подключения\n"]
    if check.token_valid is False:
        lines.append("Токен: ❌ Telegram его не принимает. Сообщите владельцу сервиса — он подключит бота заново.")
    elif check.error is not None:
        lines.append(f"Не удалось проверить: {escape(check.error)}\nПопробуйте ещё раз чуть позже.")
    else:
        lines.append("Токен: ✅ действителен")
        if check.webhook_ok is True:
            lines.append("Webhook: ✅ установлен")
        elif not check.webhook_registered:
            lines.append("Webhook: ❌ не установлен — нажмите «Перерегистрировать webhook»")
        elif check.webhook_matches is False:
            lines.append("Webhook: ⚠️ указывает на другой адрес — нажмите «Перерегистрировать webhook»")
        else:
            lines.append("Webhook: установлен")
        if check.pending_updates:
            lines.append(f"Ожидают доставки: {check.pending_updates}")
        if check.last_error_message:
            lines.append(f"Последняя ошибка доставки: {escape(check.last_error_message)}")
    if not check.is_active:
        lines.append("Статус: 🔴 бот отключён владельцем сервиса")
    return "\n".join(lines)


@router.message(F.text == BTN_MY_BOT)
async def open_my_bot(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await state.clear()
    text, markup = await _card(message, session, tenant)
    await message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


@router.callback_query(MyBotCallback.filter(F.action == "show"))
async def show_my_bot(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    if isinstance(callback.message, Message):
        text, markup = await _card(callback.message, session, tenant)
        await _edit_or_answer(callback, text, markup)
    await callback.answer()


@router.callback_query(MyBotCallback.filter(F.action == "check"))
async def check_my_bot(
    callback: CallbackQuery, session_factory: async_sessionmaker, tenant: TenantContext, alerts: AlertNotifier | None = None
) -> None:
    await callback.answer("Проверяю…")
    try:
        check = await management.check_bot(
            session_factory, TokenCipher.from_settings(), master_id=tenant.master_id, public_base_url=settings.public_base_url
        )
    except MANAGEMENT_ERRORS as exc:
        logger.warning("Проверка бота мастера %s не выполнена: %s", tenant.master_id, type(exc).__name__)
        await _edit_or_answer(
            callback, f"🔍 Проверка недоступна: {escape(str(exc))}\n\nОбратитесь к владельцу сервиса.", _back_kb()
        )
        return
    await _notify_superadmin(alerts, check)
    await _edit_or_answer(callback, _check_text(check), _back_kb())


@router.callback_query(MyBotCallback.filter(F.action == "webhook"))
async def refresh_my_webhook(
    callback: CallbackQuery, session_factory: async_sessionmaker, tenant: TenantContext, alerts: AlertNotifier | None = None
) -> None:
    await callback.answer("Регистрирую webhook…")
    try:
        row = await management.refresh_webhook(
            session_factory,
            TokenCipher.from_settings(),
            master_id=tenant.master_id,
            public_base_url=settings.public_base_url,
        )
    except MANAGEMENT_ERRORS as exc:
        logger.warning("Webhook бота мастера %s не перерегистрирован: %s", tenant.master_id, type(exc).__name__)
        if alerts is not None:
            await alerts.notify(
                alert_texts.KIND_CONNECTION_ERROR,
                tenant.master_id,
                alert_texts.connection_error_text(tenant.master_id, None, None, f"Мастер не смог перерегистрировать webhook: {exc}"),
            )
        await _edit_or_answer(
            callback,
            f"❌ Webhook не перерегистрирован: {escape(str(exc))}\n\nВладельцу сервиса отправлен сигнал.",
            _back_kb(),
        )
        return
    logger.info("Мастер %s перерегистрировал webhook бота %s", tenant.master_id, row.telegram_bot_id)
    if alerts is not None:
        alerts.clear(alert_texts.KIND_WEBHOOK_DOWN, row.telegram_bot_id)
    await _edit_or_answer(callback, "✅ Webhook перерегистрирован. Теперь можно проверить подключение.", _back_kb())
