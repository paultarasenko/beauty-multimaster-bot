import datetime as dt

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.bot.keyboards.callback_data import (
    AdminAccessRequestCallback,
    AdminBlockCallback,
    AdminClientAccessCallback,
    AdminBookingCallback,
    AdminBookingsPageCallback,
    AdminLimitCommentCallback,
    AdminLimitRequestCallback,
    AdminPortfolioCallback,
    AdminReminderCallback,
    AdminReviewDeleteCallback,
    AdminReviewsCallback,
    AdminScheduleCallback,
    ConfirmCallback,
    NavCallback,
    PortfolioCallback,
    ReportCallback,
    ReviewsCallback,
    TimeCallback,
)
from app.services.stats_service import PERIOD_3MONTHS, PERIOD_6MONTHS, PERIOD_MONTH, PERIOD_TODAY, PERIOD_WEEK

# Тексты кнопок подменю «Записи» лежат здесь, чтобы клавиатура и обработчики
# всегда использовали один и тот же текст.
BTN_BOOKINGS_WEEK = "📅 На неделю"
BTN_BOOKINGS_MONTH = "📅 На месяц"
BTN_BOOKINGS_3MONTHS = "📅 На три месяца"
BTN_BOOKINGS_ADD = "➕ Добавить запись"  # тот же текст, что в handlers/admin/add_booking.py
BTN_BOOKINGS_DELETE = "🗑️ Отменить запись"
BTN_BOOKINGS_BLOCK = "🚫 Закрыть время"
BTN_BOOKINGS_UNBLOCK = "🔓 Открыть время"
# Сообщение, с которым показывается подменю «Записи» (Telegram не показывает кнопки без текста).
BOOKINGS_MENU_PROMPT = "Выберите действие"
BTN_BOOKINGS_BACK = "⚙️ Админ-панель"  # текст должен отличаться от «⬅️ Назад» в клиентском меню «Информация» (handlers/client/content.py), иначе админ-роутер перехватит его

# Кнопки «Приветствие» (handlers/admin/about.py) и «Напоминания» (handlers/admin/reminders.py)
# в админском меню «Информация».
BTN_REMINDERS = "🔔 Напоминания"
BTN_SETTINGS = "⚙️ Настройки"  # handlers/admin/master_settings.py
BTN_MY_BOT = "🤖 Мой бот"  # handlers/admin/my_bot.py (этап 9); кнопка в «⚙️ Настройки», текстом тоже работает
BTN_GREETING = "👋 Приветствие"

# Тексты кнопок подменю «Статистика».
BTN_STATS_TODAY = "📅 За сегодня"
BTN_STATS_WEEK = "📅 За неделю"
BTN_STATS_MONTH = "📅 За месяц"
BTN_STATS_3MONTHS = "📅 За три месяца"
BTN_STATS_REPORT = "📊 Отчёт в Excel"
BTN_STATS_BACK = BTN_BOOKINGS_BACK  # та же кнопка возврата — её уже обрабатывает back_to_admin_menu в handlers/admin/bookings.py


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🕐 Расписание"), KeyboardButton(text="📅 Записи")],
        [KeyboardButton(text="💰 Услуги и цены"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="📸 Портфолио"), KeyboardButton(text="ℹ️ Информация")],
        [KeyboardButton(text="👤 Профиль клиента"), KeyboardButton(text=BTN_SETTINGS)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_info_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="✨ Обо мне"), KeyboardButton(text="🕒 Проверить время")],
        [KeyboardButton(text="⭐ Отзывы"), KeyboardButton(text="📍 Как меня найти")],
        [KeyboardButton(text=BTN_GREETING), KeyboardButton(text=BTN_REMINDERS)],
        [KeyboardButton(text=BTN_BOOKINGS_BACK)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_bookings_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_BOOKINGS_WEEK), KeyboardButton(text=BTN_BOOKINGS_BLOCK)],
        [KeyboardButton(text=BTN_BOOKINGS_MONTH), KeyboardButton(text=BTN_BOOKINGS_UNBLOCK)],
        [KeyboardButton(text=BTN_BOOKINGS_ADD), KeyboardButton(text=BTN_BOOKINGS_DELETE)],
        [KeyboardButton(text=BTN_BOOKINGS_BACK)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_stats_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_STATS_TODAY), KeyboardButton(text=BTN_STATS_WEEK)],
        [KeyboardButton(text=BTN_STATS_MONTH), KeyboardButton(text=BTN_STATS_3MONTHS)],
        [KeyboardButton(text=BTN_STATS_REPORT)],
        [KeyboardButton(text=BTN_STATS_BACK)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def bookings_to_delete_keyboard(
    items: list[tuple[int, str]], page: int = 0, has_prev: bool = False, has_next: bool = False
) -> InlineKeyboardMarkup:
    """items — список пар (id записи, подпись на кнопке) одной страницы.

    Если есть соседние страницы, внизу появляются «◀️ Раньше» и/или «Дальше ➡️»."""
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=AdminBookingCallback(booking_id=booking_id, action="delete", page=page).pack(),
            )
        ]
        for booking_id, label in items
    ]
    nav_row = []
    if has_prev:
        nav_row.append(
            InlineKeyboardButton(text="◀️ Раньше", callback_data=AdminBookingsPageCallback(page=page - 1).pack())
        )
    if has_next:
        nav_row.append(
            InlineKeyboardButton(text="Дальше ➡️", callback_data=AdminBookingsPageCallback(page=page + 1).pack())
        )
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_confirm_keyboard(booking_id: int, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=AdminBookingCallback(booking_id=booking_id, action="confirm", page=page).pack(),
                ),
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminBookingCallback(booking_id=booking_id, action="back", page=page).pack(),
                ),
            ]
        ]
    )


def blocks_to_unblock_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """items — список пар (id блокировки, подпись на кнопке)."""
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=AdminBlockCallback(block_id=block_id, action="unblock").pack(),
            )
        ]
        for block_id, label in items
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def unblock_confirm_keyboard(block_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, снять",
                    callback_data=AdminBlockCallback(block_id=block_id, action="confirm").pack(),
                ),
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminBlockCallback(block_id=block_id, action="back").pack(),
                ),
            ]
        ]
    )


def schedule_start_time_keyboard(times: list[dt.datetime]) -> InlineKeyboardMarkup:
    """Кнопки выбора времени начала рабочего дня + отдельная кнопка «сделать выходным»,
    как в выборе времени при закрытии («блокировке») времени."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in times:
        row.append(
            InlineKeyboardButton(text=t.strftime("%H:%M"), callback_data=TimeCallback(ts=int(t.timestamp())).pack())
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="🍽 Обед", callback_data=AdminScheduleCallback(action="lunch").pack()),
            InlineKeyboardButton(text="🚫 Выходной", callback_data=AdminScheduleCallback(action="day_off").pack()),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=NavCallback(target="back").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def special_day_menu_keyboard(is_open: bool, has_override: bool) -> InlineKeyboardMarkup:
    """Экран одной даты в «Особых днях»: открыть (или сменить часы), закрыть, вернуть по расписанию, назад к календарю."""
    open_text = "🕐 Изменить часы на этот день" if is_open else "🟢 Открыть на этот день"
    rows = [[InlineKeyboardButton(text=open_text, callback_data=AdminScheduleCallback(action="sd_open").pack())]]
    if is_open:
        rows.append([InlineKeyboardButton(text="🔴 Закрыть день", callback_data=AdminScheduleCallback(action="sd_close").pack())])
    if has_override:
        rows.append(
            [InlineKeyboardButton(text="↩️ Вернуть по расписанию", callback_data=AdminScheduleCallback(action="sd_reset").pack())]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=NavCallback(target="back").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_lunch_menu_keyboard(has_lunch: bool) -> InlineKeyboardMarkup:
    """Экран обеда выбранного дня: задать/изменить, убрать (если обед есть), назад к экрану дня."""
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Изменить обед" if has_lunch else "➕ Задать обед",
                callback_data=AdminScheduleCallback(action="lunch_set").pack(),
            )
        ]
    ]
    if has_lunch:
        rows.append(
            [InlineKeyboardButton(text="🗑 Убрать обед", callback_data=AdminScheduleCallback(action="lunch_remove").pack())]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=NavCallback(target="back").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_lunch_confirm_keyboard() -> InlineKeyboardMarkup:
    """Куда сохранить выбранный обед: только в этот день или во все рабочие дни, куда он помещается."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Только этот день", callback_data=AdminScheduleCallback(action="lunch_day").pack())],
            [InlineKeyboardButton(text="📅 Все рабочие дни", callback_data=AdminScheduleCallback(action="lunch_all").pack())],
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=ConfirmCallback(action="edit").pack())],
        ]
    )


def admin_reviews_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📖 Посмотреть", callback_data=ReviewsCallback(action="view").pack()),
                InlineKeyboardButton(text="🖊️ Оставить", callback_data=ReviewsCallback(action="add").pack()),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Удалить",
                    callback_data=AdminReviewDeleteCallback(review_id=0, action="list").pack(),
                ),
                InlineKeyboardButton(text="⬅️ Назад", callback_data=AdminReviewsCallback(action="back").pack()),
            ],
        ]
    )


def admin_review_delete_list_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """items — список пар (id отзыва, подпись на кнопке)."""
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=AdminReviewDeleteCallback(review_id=review_id, action="open").pack(),
            )
        ]
        for review_id, label in items
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=AdminReviewsCallback(action="back").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_review_delete_confirm_keyboard(review_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=AdminReviewDeleteCallback(review_id=review_id, action="confirm").pack(),
                ),
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminReviewDeleteCallback(review_id=0, action="list").pack(),
                ),
            ]
        ]
    )


def portfolio_admin_keyboard(
    current_id: int, prev_id: int | None, next_id: int | None
) -> InlineKeyboardMarkup:
    """Клавиатура мастера при просмотре фото портфолио: листание (если фото больше одного)
    плюс управление — добавить новое фото / удалить текущее."""
    rows: list[list[InlineKeyboardButton]] = []
    if prev_id is not None and next_id is not None:
        rows.append(
            [
                InlineKeyboardButton(text="◀️", callback_data=PortfolioCallback(item_id=prev_id).pack()),
                InlineKeyboardButton(text="▶️", callback_data=PortfolioCallback(item_id=next_id).pack()),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Название",
                callback_data=AdminPortfolioCallback(item_id=current_id, action="edit_title").pack(),
            ),
            InlineKeyboardButton(
                text="✏️ Описание",
                callback_data=AdminPortfolioCallback(item_id=current_id, action="edit_description").pack(),
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить", callback_data=AdminPortfolioCallback(item_id=0, action="add").pack()
            ),
            InlineKeyboardButton(
                text="🗑️ Удалить",
                callback_data=AdminPortfolioCallback(item_id=current_id, action="delete").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def portfolio_admin_empty_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить", callback_data=AdminPortfolioCallback(item_id=0, action="add").pack()
                )
            ]
        ]
    )


def portfolio_admin_edit_cancel_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """Кнопка отмены под сообщением «Введите новое название/описание»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖ Отмена",
                    callback_data=AdminPortfolioCallback(item_id=item_id, action="edit_cancel").pack(),
                )
            ]
        ]
    )


def portfolio_admin_delete_confirm_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=AdminPortfolioCallback(item_id=item_id, action="delete_confirm").pack(),
                ),
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=PortfolioCallback(item_id=item_id).pack()
                ),
            ]
        ]
    )


def report_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📌 За сегодня", callback_data=ReportCallback(period=PERIOD_TODAY).pack()),
                InlineKeyboardButton(text="📅 За неделю", callback_data=ReportCallback(period=PERIOD_WEEK).pack()),
            ],
            [
                InlineKeyboardButton(text="🗓️ За месяц", callback_data=ReportCallback(period=PERIOD_MONTH).pack()),
                InlineKeyboardButton(
                    text="📈 На три месяца", callback_data=ReportCallback(period=PERIOD_3MONTHS).pack()
                ),
            ],
            [InlineKeyboardButton(text="📊 На 6 месяцев", callback_data=ReportCallback(period=PERIOD_6MONTHS).pack())],
        ]
    )


def _reminder_cb(action: str, recurrence: str = "none", reminder_id: int = 0) -> str:
    return AdminReminderCallback(action=action, recurrence=recurrence, reminder_id=reminder_id).pack()


def reminders_menu_keyboard() -> InlineKeyboardMarkup:
    """Меню «Напоминания»: сверху вниз — «Каждый год»; «Раз в месяц» и «Каждый день»; «Один раз» и «Раз в неделю»;
    внизу — просмотр и удаление созданных напоминаний."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Каждый год", callback_data=_reminder_cb("new", "yearly"))],
            [
                InlineKeyboardButton(text="Раз в месяц", callback_data=_reminder_cb("new", "monthly")),
                InlineKeyboardButton(text="Каждый день", callback_data=_reminder_cb("new", "daily")),
            ],
            [
                InlineKeyboardButton(text="Один раз", callback_data=_reminder_cb("new", "once")),
                InlineKeyboardButton(text="Раз в неделю", callback_data=_reminder_cb("new", "weekly")),
            ],
            [InlineKeyboardButton(text="📋 Мои напоминания", callback_data=_reminder_cb("list"))],
        ]
    )


def reminder_input_cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопка отмены под вопросами «Введите дату / время / текст»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data=_reminder_cb("cancel"))]]
    )


def reminders_list_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """items — список пар (id напоминания, подпись на кнопке)."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=_reminder_cb("open", reminder_id=reminder_id))]
        for reminder_id, label in items
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=_reminder_cb("menu"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminders_empty_keyboard() -> InlineKeyboardMarkup:
    """Под сообщением «Напоминаний пока нет.»: кнопка ведёт в меню «Как часто напоминать?»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➕ Создать напоминание", callback_data=_reminder_cb("menu"))]]
    )


def reminder_card_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=_reminder_cb("delete", reminder_id=reminder_id)),
                InlineKeyboardButton(text="⬅️ Назад", callback_data=_reminder_cb("list")),
            ]
        ]
    )


def reminder_delete_confirm_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить", callback_data=_reminder_cb("confirm", reminder_id=reminder_id)
                ),
                InlineKeyboardButton(text="⬅️ Назад", callback_data=_reminder_cb("open", reminder_id=reminder_id)),
            ]
        ]
    )


def client_access_keyboard(user_id: int, restricted: bool = False) -> InlineKeyboardMarkup:
    """Под предупреждением об отменах. После «Ограничить» остаётся только «Подтвердить» — им можно вернуть доступ."""
    restrict = InlineKeyboardButton(
        text="🚫 Ограничить доступ",
        callback_data=AdminClientAccessCallback(user_id=user_id, action="restrict").pack(),
    )
    allow = InlineKeyboardButton(
        text="✅ Подтвердить доступ",
        callback_data=AdminClientAccessCallback(user_id=user_id, action="allow").pack(),
    )
    return InlineKeyboardMarkup(inline_keyboard=[[allow]] if restricted else [[restrict], [allow]])


def limit_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Под запросом клиента на запись сверх лимита: принять или отклонить."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять",
                    callback_data=AdminLimitRequestCallback(request_id=request_id, action="approve").pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=AdminLimitRequestCallback(request_id=request_id, action="reject").pack(),
                ),
            ]
        ]
    )


def limit_comment_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Вопрос «Желаете добавить сообщение?» после решения мастера."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да",
                    callback_data=AdminLimitCommentCallback(request_id=request_id, action="yes").pack(),
                ),
                InlineKeyboardButton(
                    text="Нет",
                    callback_data=AdminLimitCommentCallback(request_id=request_id, action="no").pack(),
                ),
            ]
        ]
    )


def access_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Под сообщением ограниченного клиента: открыть доступ или ограничить навсегда."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Открыть доступ",
                    callback_data=AdminAccessRequestCallback(request_id=request_id, action="open").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Ограничить навсегда",
                    callback_data=AdminAccessRequestCallback(request_id=request_id, action="block").pack(),
                )
            ],
        ]
    )
