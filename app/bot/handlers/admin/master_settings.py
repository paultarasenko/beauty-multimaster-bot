"""«⚙️ Настройки» мастера (этап 8): интервал записи, напоминания, лимиты, права клиентов, валюта и индивидуальные тексты.

Все значения хранятся в `master_settings` (по строке на мастера) и действуют только для этого мастера. Экран открывает
только владелец (IsAdminFilter на роутере). Каждое значение проверяется (`app/utils/master_settings.py`,
`app/utils/text_templates.py`), мастер видит понятное объяснение ошибки. Изменения применяются к НОВЫМ записям и
отправкам; уже созданные записи не меняются.
"""

import datetime as dt
import io
from dataclasses import replace
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import BTN_SETTINGS
from app.bot.keyboards.callback_data import AdminSettingsCallback, MyBotCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminSettingsFSM
from app.database.repositories.master_settings_repository import MasterSettingsRepository
from app.database.repositories.quiet_mode_repository import DeferredMessageRepository, QuietModeRepository
from app.multibot.bot_avatar import BotPhotoError, remove_bot_avatar, set_bot_avatar
from app.tenancy import TenantContext
from app.utils.master_settings import (
    ALLOWED_SLOT_INTERVALS,
    BOOL_FIELDS,
    DEFAULT_CURRENCY,
    MAX_ACTIVE_BOOKINGS_LIMIT,
    MAX_CANCEL_STREAK,
    MAX_CURRENCY_LEN,
    MIN_ACTIVE_BOOKINGS,
    MIN_CANCEL_STREAK,
    TEXT_FIELDS,
    MasterSettingsData,
    SettingError,
    client_reminder_phrase,
    parse_int_input,
    validate_setting,
)
from app.services.quiet_mode_service import is_quiet_now, load_quiet_mode
from app.utils.quiet_mode import QuietModeData, QuietModeError, format_time, format_window, validate_window
from app.utils.default_texts import default_cancellation_text, default_confirmation_text
from app.utils.money import format_money
from app.utils.reminder_texts import client_reminder_2h_text, client_reminder_24h_text
from app.utils.text_templates import (
    EXAMPLE_VALUES,
    MAX_TEMPLATE_LEN,
    TemplateError,
    fields_help,
    render_preview,
    validate_template,
)

router = Router(name="admin_master_settings")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

STALE = "Эта кнопка устарела. Откройте «⚙️ Настройки» заново."
QUICK_CURRENCIES = (DEFAULT_CURRENCY, "RUB", "USD", "EUR")
# Разделы меню настроек (field у action="menu").
SECTIONS = ("main", "reminders", "limits", "texts", "quiet")
# Сетка часов тихого режима: 24 часа, 6 в ряду. Начало обычно вечером, конец утром, поэтому сетки открываются с 18 и с 05:
# частые варианты (20–00 и 06–10) попадают в первые ряды.
QUIET_GRID_FIRST_HOUR = {"start": 18, "end": 5}
QUIET_GRID_COLUMNS = 6


def _cb(action: str, field: str = "none", value: str = "none") -> str:
    return AdminSettingsCallback(action=action, field=field, value=value).pack()


def _btn(text: str, action: str, field: str = "none", value: str = "none") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=_cb(action, field, value))


def _onoff(flag: bool) -> str:
    return "🟢 вкл" if flag else "⚪ выкл"


def _mark(selected: bool, text: str) -> str:
    return f"✅ {text}" if selected else text


def _callback_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


# --- Тексты и клавиатуры экранов ---


def _quiet_summary(quiet: QuietModeData) -> str:
    return f"🟢 {format_window(quiet.start_time, quiet.end_time)}" if quiet.enabled else "⚪ выкл"


def main_text(s: MasterSettingsData, quiet: QuietModeData | None = None) -> str:
    quiet_line = f"🌙 Тихий режим: {_quiet_summary(quiet)}\n" if quiet is not None else ""
    return (
        "⚙️ Настройки\n\n"
        f"🕐 Интервал записи: {s.slot_interval_minutes} мин\n"
        f"{quiet_line}"
        f"🔔 Напоминания: клиенту за 24 ч — {_onoff(s.reminder_24h_enabled)}, за 2 ч — {_onoff(s.reminder_2h_enabled)}, "
        f"вам за 2 ч — {_onoff(s.master_reminder_2h_enabled)}\n"
        f"📋 Лимит записей клиента: {s.max_active_bookings}; предупреждение после {s.cancel_streak_limit} отмен подряд\n"
        f"🔄 Перенос записи клиентом: {_onoff(s.allow_reschedule)}\n"
        f"⭐ Отзывы клиентов: {_onoff(s.allow_reviews)}\n"
        f"💱 Валюта: {escape(s.currency)}\n\n"
        "Изменения действуют для новых записей и сообщений; уже созданные записи не меняются."
    )


def main_keyboard(s: MasterSettingsData, quiet: QuietModeData | None = None) -> InlineKeyboardMarkup:
    quiet_label = "🌙 Тихий режим" if quiet is None else f"🌙 Тихий режим: {_quiet_summary(quiet)}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(f"🕐 Интервал записи: {s.slot_interval_minutes} мин", "menu", "interval")],
            [_btn("🔔 Напоминания", "menu", "reminders")],
            [_btn(quiet_label, "menu", "quiet")],
            [_btn("📋 Лимиты записей", "menu", "limits")],
            [_btn(f"🔄 Перенос клиентом: {_onoff(s.allow_reschedule)}", "toggle", "allow_reschedule")],
            [_btn(f"⭐ Отзывы клиентов: {_onoff(s.allow_reviews)}", "toggle", "allow_reviews")],
            [_btn(f"💱 Валюта: {s.currency}", "currency")],
            [_btn("✍️ Тексты сообщений", "menu", "texts")],
            [_btn("🖼 Фото профиля", "photo")],
            [InlineKeyboardButton(text="🤖 Мой бот", callback_data=MyBotCallback(action="show").pack())],
        ]
    )


def quiet_text(quiet: QuietModeData, active_now: bool, pending: int) -> str:
    state = "🟢 включён" if quiet.enabled else "⚪ выключен"
    lines = [
        "🌙 Тихий режим\n",
        f"Сейчас: {state}",
        f"Время: {format_window(quiet.start_time, quiet.end_time)} (каждый день, по вашему часовому поясу)",
    ]
    if quiet.enabled:
        lines.append("Сейчас действует: да 🤫" if active_now else "Сейчас действует: нет")
    if pending:
        lines.append(f"Накоплено сообщений: {pending}")
    lines.append(
        "\nВ тихом режиме бот не присылает вам уведомления: новые записи, отмены, запросы клиентов и напоминания.\n"
        "Они придут сразу, как только тихий режим закончится или вы его выключите.\n"
        "Клиенты по-прежнему могут записываться.\n"
        "Это не то же самое, что беззвучный режим вашего Telegram.\n"
        "Выберите ваше время для тихого режима."
    )
    return "\n".join(lines)


def quiet_keyboard(quiet: QuietModeData) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("⚪ Выключить" if quiet.enabled else "🟢 Включить", "quiet", "toggle")],
            [
                _btn(f"🕙 Начало: {format_time(quiet.start_time)}", "quiet", "ask", "start"),
                _btn(f"🕗 Конец: {format_time(quiet.end_time)}", "quiet", "ask", "end"),
            ],
            [_btn("⬅️ Назад", "menu", "main")],
        ]
    )


def quiet_hour_text(which: str, quiet: QuietModeData) -> str:
    if which == "start":
        title, current, other = "Начало", quiet.start_time, "конец"
        question = "Выберите час, с которого начинается тихий режим."
    else:
        title, current, other = "Конец", quiet.end_time, "начало"
        question = "Выберите час, когда тихий режим заканчивается."
    return (
        f"🌙 {title} тихого режима\n\nСейчас: {format_time(current)}.\n{question}\n"
        f"Точкой отмечен час, который уже выбран как {other}: начало и конец не могут совпадать."
    )


def quiet_hour_keyboard(which: str, quiet: QuietModeData) -> InlineKeyboardMarkup:
    """Сетка 24 часов (по 6 в ряду). Выбранный сейчас час отмечен ✅, час второй границы — точкой (он недоступен)."""
    current, other = (quiet.start_time, quiet.end_time) if which == "start" else (quiet.end_time, quiet.start_time)
    first = QUIET_GRID_FIRST_HOUR[which]
    buttons = []
    for offset in range(24):
        hour = (first + offset) % 24
        label = "·" if dt.time(hour) == other else _mark(dt.time(hour) == current, f"{hour:02d}")
        buttons.append(_btn(label, "quiet", which, f"{hour:02d}"))
    rows = [buttons[i : i + QUIET_GRID_COLUMNS] for i in range(0, len(buttons), QUIET_GRID_COLUMNS)]
    rows.append([_btn("✖ Отмена", "menu", "quiet")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def interval_text(s: MasterSettingsData) -> str:
    return (
        "🕐 Интервал записи\n\n"
        f"Сейчас: {s.slot_interval_minutes} мин. С таким шагом клиентам предлагается время начала записи, "
        "и с таким же шагом выбирается время в расписании.\n\n"
        "Уже созданные записи не меняются; новые записи будут только по новой сетке."
    )


def interval_keyboard(s: MasterSettingsData) -> InlineKeyboardMarkup:
    buttons = [
        _btn(_mark(s.slot_interval_minutes == minutes, f"{minutes} мин"), "set", "slot_interval_minutes", str(minutes))
        for minutes in ALLOWED_SLOT_INTERVALS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons, [_btn("⬅️ Назад", "menu", "main")]])


def reminders_text(s: MasterSettingsData) -> str:
    return (
        "🔔 Напоминания\n\n"
        f"Клиенту за 24 часа: {_onoff(s.reminder_24h_enabled)}\n"
        f"Клиенту за 2 часа: {_onoff(s.reminder_2h_enabled)}\n"
        f"Вам за 2 часа: {_onoff(s.master_reminder_2h_enabled)}\n\n"
        "Выключенное напоминание не создаётся для новых записей и не отправляется по уже созданным."
    )


def reminders_keyboard(s: MasterSettingsData) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"{BOOL_FIELDS[field]}: {_onoff(getattr(s, field))}", "toggle", field, "reminders")]
        for field in ("reminder_24h_enabled", "reminder_2h_enabled", "master_reminder_2h_enabled")
    ]
    rows.append([_btn("⬅️ Назад", "menu", "main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def limits_text(s: MasterSettingsData) -> str:
    return (
        "📋 Лимиты записей\n\n"
        f"Записей у одного клиента одновременно: {s.max_active_bookings}\n"
        f"Предупреждение мастеру после отмен подряд: {s.cancel_streak_limit}\n\n"
        "Лимит записей применяется к новым записям клиента; уже существующие остаются."
    )


def limits_keyboard(s: MasterSettingsData) -> InlineKeyboardMarkup:
    active_row = [
        _btn(_mark(s.max_active_bookings == n, str(n)), "set", "max_active_bookings", str(n))
        for n in range(MIN_ACTIVE_BOOKINGS, MAX_ACTIVE_BOOKINGS_LIMIT + 1)
    ]
    streak_row = [
        _btn(_mark(s.cancel_streak_limit == n, str(n)), "set", "cancel_streak_limit", str(n))
        for n in range(MIN_CANCEL_STREAK, MAX_CANCEL_STREAK + 1)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("Записей у клиента:", "menu", "limits")],
            active_row[:5],
            active_row[5:],
            [_btn("Отмен подряд до предупреждения:", "menu", "limits")],
            streak_row[:5],
            streak_row[5:],
            [_btn("⬅️ Назад", "menu", "main")],
        ]
    )


def texts_menu_text() -> str:
    return "✍️ Тексты сообщений\n\nВыберите текст. Пока вы не задали свой, используется стандартный."


def texts_menu_keyboard(s: MasterSettingsData) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"{title}{' ✏️' if getattr(s, field) is not None else ''}", "text", field)]
        for field, title in TEXT_FIELDS.items()
    ]
    rows.append([_btn("⬅️ Назад", "menu", "main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def default_text_preview(field: str, s: MasterSettingsData) -> str:
    """Стандартный текст, который получают клиенты, на примере данных (HTML уже экранирован)."""
    ex = EXAMPLE_VALUES
    money = format_money(ex["price"], s.currency)
    if field == "booking_confirmation_text":
        return default_confirmation_text(
            ex["service"], ex["date"], ex["weekday"], ex["time"], money, client_reminder_phrase(s)
        )
    if field == "cancellation_text":
        return default_cancellation_text(ex["service"], ex["date"], ex["weekday"], ex["time"], money)
    if field == "reminder_24h_text":
        return client_reminder_24h_text(ex["service"], ex["time"], ex["price"], ex["address"], currency=s.currency)
    return client_reminder_2h_text(
        ex["master_name"], ex["service"], ex["time"], ex["price"], ex["address"], currency=s.currency
    )


def text_card(field: str, s: MasterSettingsData) -> tuple[str, InlineKeyboardMarkup]:
    template = getattr(s, field)
    head = f"✍️ {TEXT_FIELDS[field]}\n\n"
    if template is None:
        body = f"Сейчас используется стандартный текст (на примере):\n\n{default_text_preview(field, s)}"
        rows = [[_btn("✏️ Задать свой текст", "text_edit", field)]]
    else:
        try:
            preview = render_preview(template)
        except TemplateError:
            preview = "текст содержит недопустимые подстановки — клиентам отправляется стандартный текст."
        body = f"Ваш текст:\n{escape(template)}\n\nТак он выглядит на примере:\n{preview}"
        rows = [[_btn("✏️ Изменить", "text_edit", field), _btn("♻️ Сбросить", "text_reset", field)]]
    rows.append([_btn("⬅️ Назад", "menu", "texts")])
    return head + body, InlineKeyboardMarkup(inline_keyboard=rows)


def currency_text(s: MasterSettingsData) -> str:
    return (
        f"💱 Валюта\n\nСейчас: {escape(s.currency)}\n\n"
        f"Выберите кнопкой или отправьте свою (до {MAX_CURRENCY_LEN} символов без пробелов, например ₽ или KZT)."
    )


def currency_keyboard() -> InlineKeyboardMarkup:
    quick = [_btn(code, "set", "currency", code) for code in QUICK_CURRENCIES]
    return InlineKeyboardMarkup(inline_keyboard=[quick, [_btn("⬅️ Назад", "menu", "main")]])


def photo_text() -> str:
    return (
        "🖼 Фото бота\n\n"
        "Это аватарка вашего бота в Telegram: её видят клиенты в шапке чата и в профиле бота.\n\n"
        "Отправьте новое фото или удалите текущее."
    )


def photo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📤 Загрузить / заменить фото", "photo_ask")],
            [_btn("🗑 Удалить фото", "photo_delete")],
            [_btn("⬅️ Назад", "menu", "main")],
        ]
    )


def input_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✖ Отмена", "cancel")]])


# --- Показ экранов ---


async def _show(target: Message, text: str, markup: InlineKeyboardMarkup, *, edit: bool) -> None:
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _render_section(
    target: Message, section: str, tenant: TenantContext, *, edit: bool, prefix: str = ""
) -> None:
    s = await tenant.get_settings()
    if section == "quiet":
        quiet, active_now, pending = await _load_quiet_screen(tenant)
        text, markup = quiet_text(quiet, active_now, pending), quiet_keyboard(quiet)
        await _show(target, prefix + text, markup, edit=edit)
        return
    if section == "interval":
        text, markup = interval_text(s), interval_keyboard(s)
    elif section == "reminders":
        text, markup = reminders_text(s), reminders_keyboard(s)
    elif section == "limits":
        text, markup = limits_text(s), limits_keyboard(s)
    elif section == "texts":
        text, markup = texts_menu_text(), texts_menu_keyboard(s)
    elif section == "currency":
        text, markup = currency_text(s), currency_keyboard()
    else:
        quiet = await load_quiet_mode(tenant.session, tenant.master_id) if tenant.session is not None else None
        text, markup = main_text(s, quiet), main_keyboard(s, quiet)
    await _show(target, prefix + text, markup, edit=edit)


async def _load_quiet_screen(tenant: TenantContext) -> tuple[QuietModeData, bool, int]:
    session = tenant.session
    if session is None:
        raise RuntimeError("TenantContext без сессии БД: тихий режим прочитать нельзя")
    quiet = await load_quiet_mode(session, tenant.master_id)
    pending = await DeferredMessageRepository(session, tenant.master_id).count()
    return quiet, await is_quiet_now(session, tenant.master_id), pending


async def _save_quiet(session: AsyncSession, tenant: TenantContext, quiet: QuietModeData, **values) -> None:
    new = replace(quiet, **values)
    validate_window(new.start_time, new.end_time)
    await QuietModeRepository(session, tenant.master_id).save(
        enabled=new.enabled, start_time=new.start_time, end_time=new.end_time
    )
    await session.commit()


async def _save(session: AsyncSession, tenant: TenantContext, **values) -> None:
    await MasterSettingsRepository(session, tenant.master_id).update_fields(**values)
    await session.commit()
    tenant.invalidate_settings()


# --- Обработчики ---


@router.message(F.text == BTN_SETTINGS)
async def open_settings(message: Message, state: FSMContext, tenant: TenantContext) -> None:
    await state.clear()
    await _render_section(message, "main", tenant, edit=False)


@router.callback_query(AdminSettingsCallback.filter(F.action == "menu"))
async def open_section(
    callback: CallbackQuery, callback_data: AdminSettingsCallback, state: FSMContext, tenant: TenantContext
) -> None:
    message = _callback_message(callback)
    if message is None or callback_data.field not in (*SECTIONS, "interval"):
        await callback.answer(STALE, show_alert=True)
        return
    await state.clear()
    await _render_section(message, callback_data.field, tenant, edit=True)
    await callback.answer()


@router.callback_query(AdminSettingsCallback.filter(F.action == "set"))
async def set_value(
    callback: CallbackQuery, callback_data: AdminSettingsCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    message = _callback_message(callback)
    field = callback_data.field
    if message is None or field not in ("slot_interval_minutes", "max_active_bookings", "cancel_streak_limit", "currency"):
        await callback.answer(STALE, show_alert=True)
        return
    try:
        raw = callback_data.value if field == "currency" else parse_int_input(callback_data.value)
        value = validate_setting(field, raw)
    except SettingError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _save(session, tenant, **{field: value})
    section = {
        "slot_interval_minutes": "interval",
        "max_active_bookings": "limits",
        "cancel_streak_limit": "limits",
        "currency": "currency",
    }[field]
    await _render_section(message, section, tenant, edit=True)
    await callback.answer("Сохранено.")


@router.callback_query(AdminSettingsCallback.filter(F.action == "toggle"))
async def toggle_value(
    callback: CallbackQuery, callback_data: AdminSettingsCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    message = _callback_message(callback)
    field = callback_data.field
    if message is None or field not in BOOL_FIELDS:
        await callback.answer(STALE, show_alert=True)
        return
    current = getattr(await tenant.get_settings(), field)
    await _save(session, tenant, **{field: validate_setting(field, not current)})
    await _render_section(message, "reminders" if callback_data.value == "reminders" else "main", tenant, edit=True)
    await callback.answer("Сохранено.")


@router.callback_query(AdminSettingsCallback.filter(F.action == "currency"))
async def ask_currency(callback: CallbackQuery, state: FSMContext, tenant: TenantContext) -> None:
    message = _callback_message(callback)
    if message is None:
        await callback.answer(STALE, show_alert=True)
        return
    await state.set_state(AdminSettingsFSM.enter_currency)
    await _render_section(message, "currency", tenant, edit=True)
    await callback.answer()


@router.message(AdminSettingsFSM.enter_currency)
async def receive_currency(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    try:
        currency = validate_setting("currency", message.text)
    except SettingError as exc:
        await message.answer(str(exc), reply_markup=input_cancel_keyboard())
        return
    await _save(session, tenant, currency=currency)
    await state.clear()
    await _render_section(message, "main", tenant, edit=False, prefix="✅ Валюта сохранена.\n\n")


@router.callback_query(AdminSettingsCallback.filter(F.action == "text"))
async def open_text_card(
    callback: CallbackQuery, callback_data: AdminSettingsCallback, state: FSMContext, tenant: TenantContext
) -> None:
    message = _callback_message(callback)
    if message is None or callback_data.field not in TEXT_FIELDS:
        await callback.answer(STALE, show_alert=True)
        return
    await state.clear()
    text, markup = text_card(callback_data.field, await tenant.get_settings())
    await message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(AdminSettingsCallback.filter(F.action == "text_edit"))
async def ask_text(
    callback: CallbackQuery, callback_data: AdminSettingsCallback, state: FSMContext
) -> None:
    message = _callback_message(callback)
    if message is None or callback_data.field not in TEXT_FIELDS:
        await callback.answer(STALE, show_alert=True)
        return
    await state.set_state(AdminSettingsFSM.enter_text)
    await state.set_data({"text_field": callback_data.field})
    await message.answer(
        f"Отправьте новый текст («{TEXT_FIELDS[callback_data.field]}»), не длиннее {MAX_TEMPLATE_LEN} символов.\n\n"
        "В текст можно вставлять данные из записи — они подставятся автоматически:\n"
        f"{fields_help()}",
        reply_markup=input_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminSettingsFSM.enter_text)
async def receive_text(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    field = (await state.get_data()).get("text_field")
    if field not in TEXT_FIELDS:
        await state.clear()
        await message.answer("Что-то пошло не так. Откройте «⚙️ Настройки» и начните заново.")
        return
    try:
        template = validate_template(message.text)
    except TemplateError as exc:
        await message.answer(escape(str(exc)), reply_markup=input_cancel_keyboard())  # в тексте может быть ввод мастера
        return
    await _save(session, tenant, **{field: template})
    await state.clear()
    text, markup = text_card(field, await tenant.get_settings())
    await message.answer(f"✅ Текст сохранён.\n\n{text}", reply_markup=markup)


@router.callback_query(AdminSettingsCallback.filter(F.action == "text_reset"))
async def reset_text(
    callback: CallbackQuery, callback_data: AdminSettingsCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    message = _callback_message(callback)
    if message is None or callback_data.field not in TEXT_FIELDS:
        await callback.answer(STALE, show_alert=True)
        return
    await MasterSettingsRepository(session, tenant.master_id).reset_fields([callback_data.field])
    await session.commit()
    tenant.invalidate_settings()
    text, markup = text_card(callback_data.field, await tenant.get_settings())
    await message.edit_text(text, reply_markup=markup)
    await callback.answer("Стандартный текст возвращён.")


# --- Тихий режим ---


@router.callback_query(AdminSettingsCallback.filter(F.action == "quiet"))
async def quiet_action(
    callback: CallbackQuery,
    callback_data: AdminSettingsCallback,
    state: FSMContext,
    session: AsyncSession,
    tenant: TenantContext,
) -> None:
    message = _callback_message(callback)
    if message is None:
        await callback.answer(STALE, show_alert=True)
        return
    quiet = await load_quiet_mode(session, tenant.master_id)
    field, value = callback_data.field, callback_data.value
    if field == "ask" and value in ("start", "end"):
        await state.clear()  # устаревшая кнопка не должна оставить мастера в незавершённом вводе другого экрана
        await message.edit_text(quiet_hour_text(value, quiet), reply_markup=quiet_hour_keyboard(value, quiet))
        await callback.answer()
        return
    if field == "toggle":
        await _save_quiet(session, tenant, quiet, enabled=not quiet.enabled)
        answer = "Тихий режим включён." if not quiet.enabled else "Тихий режим выключен."
    elif field in ("start", "end") and value.isascii() and value.isdigit() and int(value) < 24:
        hour = dt.time(int(value))
        try:
            await _save_quiet(session, tenant, quiet, **{f"{field}_time": hour})
        except QuietModeError as exc:  # час второй границы (точка в сетке): начало и конец совпали бы
            await callback.answer(str(exc), show_alert=True)
            return
        answer = f"{'Начало' if field == 'start' else 'Конец'}: {format_time(hour)}."
    else:
        await callback.answer(STALE, show_alert=True)
        return
    try:
        await _render_section(message, "quiet", tenant, edit=True)
    except TelegramBadRequest:
        pass  # экран не изменился (например, повторное нажатие на ту же устаревшую сетку)
    await callback.answer(answer)


@router.callback_query(AdminSettingsCallback.filter(F.action == "cancel"))
async def cancel_input(callback: CallbackQuery, state: FSMContext, tenant: TenantContext) -> None:
    await state.clear()
    message = _callback_message(callback)
    if message is not None:
        await _render_section(message, "main", tenant, edit=False)
    await callback.answer()


# --- Фото бота (аватарка в Telegram) ---


@router.callback_query(AdminSettingsCallback.filter(F.action == "photo"))
async def open_photo(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    if message is None:
        await callback.answer(STALE, show_alert=True)
        return
    await state.clear()
    await message.edit_text(photo_text(), reply_markup=photo_keyboard())
    await callback.answer()


@router.callback_query(AdminSettingsCallback.filter(F.action == "photo_ask"))
async def ask_photo(callback: CallbackQuery, state: FSMContext) -> None:
    message = _callback_message(callback)
    if message is None:
        await callback.answer(STALE, show_alert=True)
        return
    await state.set_state(AdminSettingsFSM.enter_photo)
    await message.answer(
        "Отправьте новое фото бота одним сообщением (как фото, не файлом). Лучше квадратное: Telegram покажет его кругом.",
        reply_markup=input_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminSettingsFSM.enter_photo, F.photo)
async def receive_photo(message: Message, state: FSMContext) -> None:
    buffer = io.BytesIO()
    await message.bot.download(message.photo[-1], destination=buffer)  # самый большой размер, формат JPEG
    try:
        await set_bot_avatar(message.bot, buffer.getvalue())
    except BotPhotoError as exc:
        await message.answer(f"❌ Фото не изменено: {escape(str(exc))}", reply_markup=input_cancel_keyboard())
        return
    await state.clear()
    await message.answer(
        "✅ Фото бота обновлено. В Telegram оно может появиться не сразу: подождите пару минут "
        "или перезайдите в чат с ботом.",
        reply_markup=photo_keyboard(),
    )


@router.message(AdminSettingsFSM.enter_photo)
async def receive_photo_wrong(message: Message) -> None:
    await message.answer(
        "Нужно именно фото. Отправьте картинку как фото (не файлом) или нажмите «Отмена».",
        reply_markup=input_cancel_keyboard(),
    )


@router.callback_query(AdminSettingsCallback.filter(F.action == "photo_delete"))
async def delete_photo(callback: CallbackQuery) -> None:
    message = _callback_message(callback)
    if message is None:
        await callback.answer(STALE, show_alert=True)
        return
    try:
        await remove_bot_avatar(message.bot)
    except BotPhotoError as exc:
        await callback.answer(f"Не удалось удалить фото: {exc}", show_alert=True)
        return
    await message.answer("✅ Фото бота удалено.", reply_markup=photo_keyboard())
    await callback.answer()
