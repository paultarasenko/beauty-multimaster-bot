import calendar
import datetime as dt
from collections.abc import Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.bot.keyboards.callback_data import (
    BookingActionCallback,
    CalendarCallback,
    ConfirmCallback,
    DateCallback,
    DatesPageCallback,
    MasterRequestCallback,
    NavCallback,
    PortfolioCallback,
    ReviewRatingCallback,
    ReviewsCallback,
    ReviewsPageCallback,
    ScheduleBookCallback,
    ServiceCallback,
    TimeCallback,
)
from app.database.models import Service
from app.utils.duration import format_duration
from app.utils.master_settings import DEFAULT_CURRENCY
from app.utils.money import format_money


def main_menu_keyboard(has_active_booking: bool, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📅 Записаться"), KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="💰 Услуги и цены"), KeyboardButton(text="📸 Портфолио")],
        [KeyboardButton(text="ℹ️ Информация")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="👑 Профиль мастера")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def info_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="✨ Обо мне"), KeyboardButton(text="🕒 Проверить время")],
        [KeyboardButton(text="⭐ Отзывы"), KeyboardButton(text="📍 Как меня найти")],
        [KeyboardButton(text="📞 Связаться со мной"), KeyboardButton(text="🏠 Главное меню")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="👑 Профиль мастера")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def services_keyboard(services: list[Service], currency: str = DEFAULT_CURRENCY) -> InlineKeyboardMarkup:
    """Кнопки услуг. Название показывается целиком, как его ввёл мастер (в том числе с эмодзи в начале)."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{s.name} — {format_money(s.price, currency)} / {format_duration(s.duration_minutes)}",
                callback_data=ServiceCallback(service_id=s.id).pack(),
            )
        ]
        for s in services
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


WEEKDAY_NAMES_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DATE_BUTTON_FORMAT = "%d.%m"  # число и месяц на кнопке даты: «Чт 24.09». Для порядка «09.24» — "%m.%d"


MONTH_TITLES_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]  # fmt: skip
CALENDAR_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
CALENDAR_BLANK = " "  # пустая ячейка: Telegram не принимает кнопку с пустым текстом
CALENDAR_FILL = "\u2800"  # невидимый символ (не пробел, поэтому Telegram его не обрезает) для выравнивания ширины
CALENDAR_ARROW_BLANK = CALENDAR_FILL * 2  # вместо недоступной стрелки месяца: по ширине близко к ◀️ / ▶️
CALENDAR_TITLE_LENGTH = max(len(name) for name in MONTH_TITLES_RU) + len(" '00")  # длина самого длинного заголовка
CALENDAR_PAST = "·"  # день уже прошёл
CALENDAR_BUSY_MARK = "🔴"  # день рабочий, но свободного времени нет
CALENDAR_OFF_MARK = "➖"  # выходной мастера

CALENDAR_FREE = "free"
CALENDAR_BUSY = "busy"
CALENDAR_OFF = "off"


def calendar_title(year: int, month: int) -> str:
    """Заголовок «Месяц 'гг» (Февраль '26), добитый невидимыми символами по краям до одной длины для всех месяцев.

    Telegram подбирает ширину кнопок и всего блока по тексту; без выравнивания блок сужается и расширяется при листании.
    """
    title = f"{MONTH_TITLES_RU[month - 1]} '{year % 100:02d}"
    padding = max(CALENDAR_TITLE_LENGTH - len(title), 0)
    left = padding // 2
    return CALENDAR_FILL * left + title + CALENDAR_FILL * (padding - left)


def _calendar_grid(
    year: int,
    month: int,
    *,
    today: dt.date,
    last_date: dt.date,
    day_button: Callable[[dt.date], InlineKeyboardButton],
    with_back: bool = False,
) -> InlineKeyboardMarkup:
    """Каркас календаря месяца: стрелки и название, дни недели, сетка, «✖ Отмена».

    Прошедшие дни рисуются точкой, дни после `last_date` и пустые ячейки — пробелом; кнопку каждого дня от `today`
    до `last_date` строит `day_button`. `with_back` добавляет в нижний ряд «⬅️ Назад» (NavCallback back) слева от «Отмены».
    """
    noop = CalendarCallback(action="noop").pack()

    def blank() -> InlineKeyboardButton:
        return InlineKeyboardButton(text=CALENDAR_BLANK, callback_data=noop)

    def arrow_blank() -> InlineKeyboardButton:
        return InlineKeyboardButton(text=CALENDAR_ARROW_BLANK, callback_data=noop)

    def month_button(target: dt.date, text: str) -> InlineKeyboardButton:
        data = CalendarCallback(action="month", year=target.year, month=target.month).pack()
        return InlineKeyboardButton(text=text, callback_data=data)

    first_day = dt.date(year, month, 1)
    prev_month = first_day - dt.timedelta(days=1)  # любой день предыдущего месяца
    next_month = (first_day + dt.timedelta(days=31)).replace(day=1)
    has_prev = (year, month) > (today.year, today.month)
    has_next = next_month <= last_date

    rows = [
        [
            month_button(prev_month, "◀️") if has_prev else arrow_blank(),
            InlineKeyboardButton(text=calendar_title(year, month), callback_data=noop),
            month_button(next_month, "▶️") if has_next else arrow_blank(),
        ],
        [InlineKeyboardButton(text=name, callback_data=noop) for name in CALENDAR_WEEKDAYS_RU],
    ]

    for week in calendar.monthcalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for number in week:
            if number == 0:
                row.append(blank())
                continue
            day = dt.date(year, month, number)
            if day < today:
                row.append(InlineKeyboardButton(text=CALENDAR_PAST, callback_data=noop))
            elif day > last_date:
                row.append(blank())
            else:
                row.append(day_button(day))
        rows.append(row)

    footer = [InlineKeyboardButton(text="✖ Отмена", callback_data=CalendarCallback(action="cancel").pack())]
    if with_back:
        footer.insert(0, InlineKeyboardButton(text="⬅️ Назад", callback_data=NavCallback(target="back").pack()))
    rows.append(footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calendar_keyboard(
    year: int, month: int, *, today: dt.date, last_date: dt.date, states: dict[dt.date, str], with_back: bool = False
) -> InlineKeyboardMarkup:
    """Календарь месяца для «Проверить время» и «Закрыть время».

    `today` — сегодняшняя дата мастера (свободный сегодняшний день в скобках), `last_date` — последний день, на который
    можно записаться; `states` — состояние дней месяца с `today` по `last_date`: CALENDAR_FREE / CALENDAR_BUSY / CALENDAR_OFF.
    Свободный день открывает выбор даты (DateCallback). Занятый (🔴) и выходной (➖) показываются без числа, только
    символом, и отвечают всплывающим сообщением.
    """

    def day_button(day: dt.date) -> InlineKeyboardButton:
        state = states.get(day, CALENDAR_FREE)
        if state == CALENDAR_BUSY:
            return InlineKeyboardButton(text=CALENDAR_BUSY_MARK, callback_data=CalendarCallback(action="busy").pack())
        if state == CALENDAR_OFF:
            return InlineKeyboardButton(text=CALENDAR_OFF_MARK, callback_data=CalendarCallback(action="off").pack())
        label = f"[{day.day}]" if day == today else str(day.day)
        return InlineKeyboardButton(text=label, callback_data=DateCallback(date=day.isoformat()).pack())

    return _calendar_grid(year, month, today=today, last_date=last_date, day_button=day_button, with_back=with_back)


def calendar_pick_keyboard(
    year: int, month: int, *, today: dt.date, last_date: dt.date, marks: dict[dt.date, str]
) -> InlineKeyboardMarkup:
    """Календарь выбора любой даты (для «Особых дней» мастера): каждый день от `today` кликабелен и открывает DateCallback.

    `marks` — пометка дней с исключением из расписания (✅ открыт, 🔴 закрыт). Такой день рисуется одним символом без
    числа, но остаётся кликабельным: его можно изменить или вернуть по расписанию.
    """

    def day_button(day: dt.date) -> InlineKeyboardButton:
        label = marks.get(day) or (f"[{day.day}]" if day == today else str(day.day))
        return InlineKeyboardButton(text=label, callback_data=DateCallback(date=day.isoformat()).pack())

    return _calendar_grid(year, month, today=today, last_date=last_date, day_button=day_button)


def dates_keyboard(
    available_dates: list[dt.date], page: int = 0, total_pages: int = 1, with_back: bool = False
) -> InlineKeyboardMarkup:
    """Кнопки дат. Если страниц больше одной, в нижнем ряду появляются «◀️ Раньше» и «Дальше ➡️»."""
    rows = []
    row: list[InlineKeyboardButton] = []
    for d in available_dates:
        label = f"{WEEKDAY_NAMES_RU[d.weekday()]} {d.strftime(DATE_BUTTON_FORMAT)}"
        row.append(InlineKeyboardButton(text=label, callback_data=DateCallback(date=d.isoformat()).pack()))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(text="◀️ Раньше", callback_data=DatesPageCallback(page=page - 1).pack())
        )
    if with_back:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=NavCallback(target="back").pack()))
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(text="Дальше ➡️", callback_data=DatesPageCallback(page=page + 1).pack())
        )
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def times_keyboard(available_times: list[dt.datetime], with_main_menu: bool = True) -> InlineKeyboardMarkup:
    rows = []
    row: list[InlineKeyboardButton] = []
    for t in available_times:
        row.append(
            InlineKeyboardButton(text=t.strftime("%H:%M"), callback_data=TimeCallback(ts=int(t.timestamp())).pack())
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav_row = [InlineKeyboardButton(text="⬅️ Назад", callback_data=NavCallback(target="back").pack())]
    if with_main_menu:
        nav_row.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data=NavCallback(target="main_menu").pack()))
    rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def portfolio_nav_keyboard(prev_id: int, next_id: int) -> InlineKeyboardMarkup:
    """Листание портфолио по кругу: ◀️ — предыдущее фото, ▶️ — следующее."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="◀️", callback_data=PortfolioCallback(item_id=prev_id).pack()),
                InlineKeyboardButton(text="▶️", callback_data=PortfolioCallback(item_id=next_id).pack()),
            ]
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=ConfirmCallback(action="yes").pack()),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=ConfirmCallback(action="edit").pack()),
            ]
        ]
    )


def my_booking_keyboard(
    booking_id: int, contact_username: str | None = None, allow_reschedule: bool = True
) -> InlineKeyboardMarkup:
    """allow_reschedule=False (мастер отключил перенос записей клиентом): кнопки «Перенести» нет."""
    row = []
    if allow_reschedule:
        row.append(
            InlineKeyboardButton(
                text="🔄 Перенести",
                callback_data=BookingActionCallback(booking_id=booking_id, action="reschedule").pack(),
            )
        )
    row.append(
        InlineKeyboardButton(
            text="❌ Отменить",
            callback_data=BookingActionCallback(booking_id=booking_id, action="cancel").pack(),
        )
    )
    rows = [row]
    if contact_username:
        rows.append(
            [InlineKeyboardButton(text="📞 Связаться с мастером", url=f"https://t.me/{contact_username}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_confirm_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить",
                    callback_data=BookingActionCallback(booking_id=booking_id, action="cancel_confirm").pack(),
                ),
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=BookingActionCallback(booking_id=booking_id, action="cancel_back").pack(),
                ),
            ]
        ]
    )


def reviews_entry_keyboard(can_write: bool = True) -> InlineKeyboardMarkup:
    """can_write=False (ограниченный или заблокированный клиент): только «Посмотреть», без «Оставить»."""
    row = [InlineKeyboardButton(text="📖 Посмотреть", callback_data=ReviewsCallback(action="view").pack())]
    if can_write:
        row.append(InlineKeyboardButton(text="🖊️ Оставить", callback_data=ReviewsCallback(action="add").pack()))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def reviews_list_keyboard(has_more: bool, next_offset: int, can_write: bool = True) -> InlineKeyboardMarkup | None:
    """can_write=False: без «Оставить»; если и «Читать ещё» не нужна — клавиатуры нет вовсе (None)."""
    row: list[InlineKeyboardButton] = []
    if has_more:
        row.append(
            InlineKeyboardButton(
                text="⬅️ Читать ещё", callback_data=ReviewsPageCallback(offset=next_offset).pack()
            )
        )
    if can_write:
        row.append(InlineKeyboardButton(text="🖊️ Оставить", callback_data=ReviewsCallback(action="add").pack()))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


def review_rating_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⭐" * i, callback_data=ReviewRatingCallback(rating=i).pack())]
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def already_reviewed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Редактировать", callback_data=ReviewsCallback(action="edit").pack()),
                InlineKeyboardButton(text="⬅️ Назад", callback_data=ReviewsCallback(action="back").pack()),
            ]
        ]
    )


def main_menu_inline_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data=NavCallback(target="main_menu").pack())]]
    )


def start_booking_inline_keyboard(selected_date: dt.date | None = None) -> InlineKeyboardMarkup:
    """Кнопка «Записаться» под фото расписания (см. check_time.py).

    С датой — по нажатию бот спросит «Другой день / Этот день» (booking_day_choice_keyboard); без даты —
    сразу запускает тот же сценарий, что и обычная кнопка «📅 Записаться» в главном меню."""
    callback_data = (
        NavCallback(target="start_booking").pack()
        if selected_date is None
        else ScheduleBookCallback(action="ask", date=selected_date.isoformat()).pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📅 Записаться", callback_data=callback_data)]])


def booking_day_choice_keyboard(selected_date: dt.date) -> InlineKeyboardMarkup:
    """Вместо кнопки «Записаться» под расписанием: слева — выбрать другой день, справа — записаться на этот."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Другой день", callback_data=NavCallback(target="start_booking").pack()),
                InlineKeyboardButton(
                    text="Этот день",
                    callback_data=ScheduleBookCallback(action="day", date=selected_date.isoformat()).pack(),
                ),
            ]
        ]
    )


def limit_reached_keyboard() -> InlineKeyboardMarkup:
    """Кнопка под сообщением о лимите записей."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Написать мастеру",
                    callback_data=MasterRequestCallback(action="write").pack(),
                )
            ]
        ]
    )


def master_request_cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Отмена» под просьбой написать сообщение мастеру."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖ Отмена", callback_data=MasterRequestCallback(action="cancel").pack())]
        ]
    )
