"""Тексты, клавиатуры и callback-данные суперадмин-бота (всё на русском)."""

import datetime as dt
from html import escape
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.config.settings import settings
from app.multibot import management
from app.reference.service import REFERENCE_MASTER_ID
from app.superadmin import health, trial
from app.superadmin.service import ActionRecord, MasterCard, MasterRow, Summary

BTN_SUMMARY = "📊 Сводка"
BTN_MASTERS = "👥 Мастера"
BTN_CREATE = "➕ Создать мастера"
BTN_JOURNAL = "📜 Журнал"
MENU_BUTTONS = (BTN_SUMMARY, BTN_MASTERS, BTN_CREATE, BTN_JOURNAL)

BTN_SAVE_REFERENCE = "💾 Сохранить как эталон"  # у мастера-эталона (№1): его состояние становится эталоном
BTN_RESET_REFERENCE = "🧹 Сбросить к эталону"  # у остальных мастеров: данные заменяются эталоном

PAGE_SIZE = 8

ACTION_TITLES = {
    "create_master": "создан мастер",
    "add_bot": "подключён бот",
    "remove_bot": "удалён бот",
    "disable_master": "мастер отключён",
    "enable_master": "мастер включён",
    "disable_bot": "бот выключен",
    "enable_bot": "бот включён",
    "check_bot": "проверка бота",
    "refresh_webhook": "webhook перерегистрирован",
    "rotate_webhook_secret": "webhook: новый секрет",
    "extend_trial": "срок продлён",
    "clear_trial": "срок снят (без срока)",
    "set_trial": "задан срок",
    "set_owner": "сменён владелец",
    "reset_reference": "мастер сброшен к эталону",
    "overwrite_reference": "эталон сохранён",
}


class SaCallback(CallbackData, prefix="sa"):
    # action: list | need | card | check | wh | whrot | connect | cancel | ask_md | do_md | en_m | en_b | ask_rm | do_rm
    #         | more (подменю «Ещё» в карточке) | mj (журнал действий над мастером) | find (поиск мастера в списке)
    #         | trn (срок нового мастера: arg = дни, 0 — без срока) | trnc (новый мастер: своё число)
    #         | ext (продлить: arg = дни) | extc (продлить: своё число) | notrial (снять срок) | chown (сменить владельца)
    #         | ask_rs / do_rs (сбросить мастера к эталону: подтверждение / выполнение)
    #         | ask_ow / do_ow (сохранить мастера №1 как эталон: подтверждение / выполнение)
    action: str
    master_id: int = 0
    arg: int = 0  # номер страницы для action="list"/"need"; число дней для "trn"/"ext"


class SaFSM(StatesGroup):
    waiting_owner_id = State()
    waiting_token = State()  # в данных состояния только master_id: токен нигде не сохраняется
    waiting_new_trial = State()  # создание мастера: id владельца уже в данных, ждём выбор срока (кнопки или своё число)
    waiting_extend_days = State()  # продление: master_id в данных, ждём число дней
    waiting_new_owner = State()  # смена владельца: master_id в данных, ждём Telegram id нового владельца
    waiting_search = State()  # поиск мастера: ждём Telegram id владельца, номер мастера или @username бота


def _cb(action: str, master_id: int = 0, arg: int = 0) -> str:
    return SaCallback(action=action, master_id=master_id, arg=arg).pack()


def _btn(text: str, action: str, master_id: int = 0, arg: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=_cb(action, master_id, arg))


def menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SUMMARY), KeyboardButton(text=BTN_MASTERS)],
        [KeyboardButton(text=BTN_CREATE), KeyboardButton(text=BTN_JOURNAL)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _fmt_dt(value: dt.datetime) -> str:
    return value.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y %H:%M")


def _bot_label(row: MasterRow) -> str:
    if row.telegram_bot_id is None:
        return "бота нет"
    name = f"@{escape(row.bot_username)}" if row.bot_username else f"id {row.telegram_bot_id}"
    return f"{name} ({'активен' if row.bot_active else 'выключен'})"


def webhook_label(state: str | None) -> str:
    if settings.run_mode != "webhook":
        return "—"
    return {health.STATE_OK: "✅", health.STATE_PROBLEM: "⚠️", health.STATE_UNKNOWN: "❔"}.get(state or "", "❔")


def summary_text(s: Summary) -> str:
    return (
        "📊 Сводка\n\n"
        f"Мастеров: {s.masters_total} (подключено {s.masters_active}, отключено {s.masters_inactive})\n"
        f"Боты: активных {s.bots_active}, неактивных {s.bots_inactive}\n"
        f"Клиентов (связей с мастерами): {s.clients_total}\n"
        f"Записей: {s.bookings_total} (подтверждённых {s.bookings_confirmed})"
    )


def _trial_suffix(row: MasterRow, today: dt.date) -> str:
    status = trial.trial_status(row.trial_ends_on, today)
    icon = trial.status_icon(status)
    return f" · {icon + ' ' if icon else ''}{trial.status_text(status)}"


def _master_line(row: MasterRow, states: dict[int, str | None], today: dt.date) -> str:
    status = "🟢" if row.is_active else "🔴"
    wh = f", webhook {webhook_label(states.get(row.master_id))}" if row.telegram_bot_id is not None else ""
    return f"{status} №{row.master_id} · владелец {row.owner_telegram_id or '—'} · {_bot_label(row)}{wh}{_trial_suffix(row, today)}"


def _master_button(row: MasterRow) -> list[InlineKeyboardButton]:
    label = f"{'🟢' if row.is_active else '🔴'} №{row.master_id}" + (f" · @{row.bot_username}" if row.bot_username else "")
    return [_btn(label, "card", row.master_id)]


def masters_text(
    rows: list[MasterRow], page: int, states: dict[int, str | None], *, only_needs: bool = False, today: dt.date | None = None
) -> str:
    today = today or trial.superadmin_today()
    if not rows:
        if only_needs:
            return "Мастеров, которым нужно решение по сроку, нет."
        return "Мастеров пока нет. Нажмите «➕ Создать мастера»."
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    title = "⚠️ Требуют решения" if only_needs else "👥 Мастера"
    lines = [f"{title} (стр. {page + 1}/{pages}, всего {len(rows)})\n"]
    for row in rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        lines.append(_master_line(row, states, today))
    lines.append("\nВыберите мастера кнопкой ниже.")
    return "\n".join(lines)


def masters_keyboard(rows: list[MasterRow], page: int, *, only_needs: bool = False, needs_count: int = 0) -> InlineKeyboardMarkup:
    chunk = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    buttons = [_master_button(r) for r in chunk]
    page_action = "need" if only_needs else "list"
    nav = []
    if page > 0:
        nav.append(_btn("◀️ Назад", page_action, arg=page - 1))
    if (page + 1) * PAGE_SIZE < len(rows):
        nav.append(_btn("Дальше ▶️", page_action, arg=page + 1))
    if nav:
        buttons.append(nav)
    buttons.append([_btn("🔎 Найти мастера", "find")])
    if only_needs:
        buttons.append([_btn("⬅️ Все мастера", "list")])
    else:
        buttons.append([_btn(f"⚠️ Требуют решения ({needs_count})", "need")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---- поиск мастера ----


def search_prompt_text() -> str:
    return (
        "🔎 Поиск мастера\n\nПришлите Telegram id владельца, номер мастера или @username бота (достаточно части username)."
    )


def search_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Все мастера", "list")]])


def search_text(query: str, rows: list[MasterRow], states: dict[int, str | None], today: dt.date | None = None) -> str:
    today = today or trial.superadmin_today()
    if not rows:
        return f"🔎 По запросу «{escape(query)}» мастеров не найдено. Проверьте id или @username."
    lines = [f"🔎 По запросу «{escape(query)}» найдено: {len(rows)}\n"]
    lines += [_master_line(row, states, today) for row in rows[:PAGE_SIZE]]
    if len(rows) > PAGE_SIZE:
        lines.append(f"\nПоказаны первые {PAGE_SIZE}. Уточните запрос.")
    return "\n".join(lines)


def search_keyboard(rows: list[MasterRow]) -> InlineKeyboardMarkup:
    buttons = [_master_button(r) for r in rows[:PAGE_SIZE]]
    buttons.append([_btn("🔎 Новый поиск", "find")])
    buttons.append([_btn("⬅️ Все мастера", "list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def card_text(
    card: MasterCard,
    webhook_state: str | None,
    today: dt.date | None = None,
    *,
    reference_saved_at: dt.datetime | None = None,
) -> str:
    """Текст карточки. У мастера-эталона (№1) показывается, когда сохранён действующий эталон."""
    row = card.row
    trial_line = f"Срок: {trial.status_text(trial.trial_status(row.trial_ends_on, today or trial.superadmin_today()))}\n"
    wh = f"\nWebhook: {webhook_label(webhook_state)}" if row.telegram_bot_id is not None else ""
    reference_line = ""
    if row.master_id == REFERENCE_MASTER_ID:
        saved = f"сохранён {_fmt_dt(reference_saved_at)}" if reference_saved_at else "ещё не сохранён"
        reference_line = f"Эталон: {saved}\n"
    return (
        f"👤 Мастер №{row.master_id}\n\n"
        f"Владелец (Telegram id): {row.owner_telegram_id or '—'}\n"
        f"Статус: {'🟢 подключён' if row.is_active else '🔴 отключён'}\n"
        f"{trial_line}"
        f"Бот: {_bot_label(row)}{wh}\n"
        f"{reference_line}"
        f"Клиентов: {card.clients}\n"
        f"Записей: {card.bookings} (подтверждённых {card.bookings_confirmed})\n"
        f"Создан: {_fmt_dt(card.created_at)}"
    )


def card_keyboard(card: MasterCard) -> InlineKeyboardMarkup:
    """Карточка мастера: то, что нужно каждый день. Редкие и технические действия — в подменю «🛠 Ещё» (`more_keyboard`)."""
    row, mid = card.row, card.row.master_id
    rows: list[list[InlineKeyboardButton]] = []
    if row.telegram_bot_id is None:
        rows.append([_btn("🤖 Подключить бота", "connect", mid)])
    else:
        rows.append([_btn("🔍 Проверить подключение", "check", mid)])
        if not row.bot_active:  # выключенным бот бывает, только если его выключила система (например, токен отвергнут)
            rows.append([_btn("▶️ Включить бота", "en_b", mid)])
    rows.append(
        [_btn(f"➕ +{days} дн.", "ext", mid, days) for days in trial.QUICK_DAYS] + [_btn("✏️ Своё число", "extc", mid)]
    )
    if row.trial_ends_on is not None:
        rows.append([_btn("♾ Оставить без срока", "notrial", mid)])
    rows.append([_btn("⏸ Отключить мастера", "ask_md", mid)] if row.is_active else [_btn("▶️ Включить мастера", "en_m", mid)])
    if row.telegram_bot_id is not None:  # фото эталона скачиваются и загружаются через бота мастера
        if mid == REFERENCE_MASTER_ID:  # мастер-эталон: его состояние становится эталоном, сам он не сбрасывается
            rows.append([_btn(BTN_SAVE_REFERENCE, "ask_ow", mid)])
        else:
            rows.append([_btn(BTN_RESET_REFERENCE, "ask_rs", mid)])
    rows.append([_btn("📜 Журнал", "mj", mid), _btn("🛠 Ещё", "more", mid)])
    rows.append([_btn("⬅️ К списку", "list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def more_text(card: MasterCard) -> str:
    return f"🛠 Дополнительные действия · мастер №{card.row.master_id}"


def more_keyboard(card: MasterCard) -> InlineKeyboardMarkup:
    """Редкие действия: webhook, смена владельца, удаление бота."""
    row, mid = card.row, card.row.master_id
    rows: list[list[InlineKeyboardButton]] = []
    if row.telegram_bot_id is not None:
        rows.append([_btn("🔄 Перерегистрировать webhook", "wh", mid), _btn("🔐 С новым секретом", "whrot", mid)])
    rows.append([_btn("👤 Сменить владельца", "chown", mid)])
    if row.telegram_bot_id is not None:
        rows.append([_btn("🗑 Удалить бота", "ask_rm", mid)])
    rows.append([_btn("⬅️ К карточке мастера", "card", mid)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_card_keyboard(master_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К карточке мастера", "card", master_id)]])


def _saved_at_text(saved_at: dt.datetime | None) -> str:
    return f"сохранён {_fmt_dt(saved_at)}" if saved_at else "ещё не сохранялся"


def reset_confirm(card: MasterCard, saved_at: dt.datetime | None) -> tuple[str, InlineKeyboardMarkup]:
    """Подтверждение «Сбросить к эталону»: что будет удалено (числа из карточки), что заменено и от какой даты эталон."""
    mid = card.row.master_id
    if saved_at is None:
        return (
            f"Эталон ещё не сохранён, сбрасывать мастера №{mid} не к чему.\n\n"
            f"Откройте карточку мастера №{REFERENCE_MASTER_ID} и нажмите «{BTN_SAVE_REFERENCE}».",
            back_to_card_keyboard(mid),
        )
    warning = ""
    yes = "Да, сбросить к эталону"
    if card.bookings_upcoming:
        warning = (
            f"⚠️ У мастера есть будущие подтверждённые записи: {card.bookings_upcoming}. Они будут удалены, клиенты об этом "
            "не узнают. Если это не тестовый мастер, не продолжайте.\n\n"
        )
        yes = "Да, удалить записи и сбросить"
    text = (
        f"{warning}"
        f"Сбросить мастера №{mid} к эталону?\n\n"
        f"Эталон — копия мастера №{REFERENCE_MASTER_ID}, {_saved_at_text(saved_at)}.\n\n"
        f"Будет удалено без возможности восстановления: записей — {card.bookings} (подтверждённых {card.bookings_confirmed}), "
        f"клиентов — {card.clients}, а также заблокированное время, напоминания и заявки. Клиентам уведомления не отправляются.\n\n"
        "Будет заменено эталоном: профиль и его фото, услуги и цены, отзывы, портфолио, расписание (с обедом), "
        "настройки и тексты сообщений, фото бота (аватарка).\n\n"
        "Владелец, бот, webhook и срок пробного периода не меняются. Фото портфолио бот загрузит себе заново: "
        "отправит их вам и сразу удалит сообщения."
    )
    return text, confirm_keyboard(yes, "do_rs", mid)


def overwrite_confirm(master_id: int, saved_at: dt.datetime | None) -> tuple[str, InlineKeyboardMarkup]:
    """Подтверждение «Сохранить как эталон»: что войдёт в эталон и какой эталон будет заменён."""
    text = (
        f"Сохранить мастера №{master_id} как эталон?\n\n"
        f"Текущее состояние мастера №{master_id} станет новым эталоном: профиль и его фото, услуги и цены, отзывы, "
        "портфолио, расписание (с обедом), настройки и тексты сообщений, фото бота. Записи и клиенты в эталон не попадают. "
        f"Мастер №{master_id} при этом не меняется.\n\n"
        f"Действующий эталон: {_saved_at_text(saved_at)}. Он будет заменён без возможности восстановления.\n\n"
        f"Другие мастера получат этот эталон по кнопке «{BTN_RESET_REFERENCE}»."
    )
    return text, confirm_keyboard("Да, сохранить", "do_ow", master_id)


def check_keyboard(check: management.BotCheck) -> InlineKeyboardMarkup:
    """Под результатом проверки: если webhook не в порядке — кнопка его исправить, затем возврат к карточке."""
    rows: list[list[InlineKeyboardButton]] = []
    if settings.run_mode == "webhook" and check.token_valid is True and check.error is None and check.webhook_ok is False:
        rows.append([_btn("🔄 Исправить webhook", "wh", check.master_id)])
    rows.append([_btn("⬅️ К карточке мастера", "card", check.master_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(yes_text: str, yes_action: str, master_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(yes_text, yes_action, master_id), _btn("Отмена", "card", master_id)]]
    )


def cancel_keyboard(master_id: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("Отмена", "card" if master_id else "cancel", master_id)]])


def check_text(check: management.BotCheck) -> str:
    name = f"@{escape(check.bot_username)}" if check.bot_username else f"id {check.telegram_bot_id}"
    lines = [f"🔍 Проверка бота {name} (мастер №{check.master_id})\n"]
    if check.token_valid is False:
        lines.append("Токен: ❌ Telegram его отвергает (Unauthorized)")
    elif check.token_valid:
        lines.append("Токен: ✅ действителен")
    if check.error and check.token_valid is not False:
        lines.append(f"Не удалось проверить: {escape(check.error)}")
    elif check.token_valid:
        if check.webhook_ok is True:
            lines.append("Webhook: ✅ установлен на наш адрес")
        elif not check.webhook_registered:
            lines.append("Webhook: ❌ не установлен")
        elif check.webhook_matches is False:
            lines.append("Webhook: ⚠️ установлен на другой адрес")
        else:
            lines.append("Webhook: установлен (адрес не сверен: PUBLIC_BASE_URL не задан)")
        if check.pending_updates is not None:
            lines.append(f"Не доставлено апдейтов: {check.pending_updates}")
        if check.last_error_message:
            when = f" ({_fmt_dt(check.last_error_at)})" if check.last_error_at else ""
            lines.append(f"Последняя ошибка доставки{when}: {escape(check.last_error_message)}")
    lines.append(f"Бот в БД: {'активен' if check.is_active else 'выключен'}")
    return "\n".join(lines)


def journal_text(records: list[ActionRecord], master_id: int | None = None) -> str:
    """Журнал действий; с `master_id` — только над одним мастером (тогда номер мастера в строках не повторяется)."""
    scope = f" · мастер №{master_id}" if master_id is not None else ""
    if not records:
        return f"📜 Журнал{scope} пуст."
    lines = [f"📜 Последние действия{scope}\n"]
    for r in records:
        target = f" · мастер №{r.target_master_id}" if r.target_master_id is not None and master_id is None else ""
        bot = f" · бот {r.target_bot_id}" if r.target_bot_id is not None else ""
        detail = f"\n    {escape(r.detail)}" if r.detail else ""
        lines.append(
            f"{_fmt_dt(r.created_at)} · {r.actor_telegram_id} · {ACTION_TITLES.get(r.action, escape(r.action))}"
            f"{target}{bot} · {'✅' if r.result == 'ok' else '❌'}{detail}"
        )
    return "\n".join(lines)


# ---- срок пробного периода ----


def new_trial_text() -> str:
    default = settings.trial_default_days
    hint = f"\nПо умолчанию (из настроек) — {default} дн. (отмечено ⭐); применяется только если вы выберете его сами." if default else ""
    return (
        "На какой срок подключить мастера на пробный период?\n\n"
        "Срок ничего не отключает сам: к его концу вы получите напоминание и решите, оставить мастера, продлить или отключить."
        f"{hint}"
    )


def new_trial_keyboard() -> InlineKeyboardMarkup:
    default = settings.trial_default_days
    options = list(trial.QUICK_DAYS)
    if default and default not in options:
        options.append(default)

    def label(days: int) -> str:
        return f"{'⭐ ' if days == default else ''}{days} дней"

    rows = [[_btn(label(days), "trn", arg=days) for days in options[:3]]]
    if len(options) > 3:
        rows.append([_btn(label(d), "trn", arg=d) for d in options[3:]])
    rows.append([_btn("Без срока", "trn", arg=0), _btn("✏️ Своё число", "trnc")])
    rows.append([_btn("Отмена", "cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
