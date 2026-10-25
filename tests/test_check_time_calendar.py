"""«Проверить время»: календарь месяца вместо сетки дат. Клавиатура — без БД, обработчики — настоящий диспетчер и БД."""

import calendar
import datetime as dt

import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage

from app.bot.handlers.client import check_time as client_check_time
from app.bot.calendar_view import BOOKING_HORIZON_DAYS, calendar_text
from app.bot.keyboards.callback_data import CalendarCallback, DateCallback
from app.bot.keyboards.client_keyboards import (
    CALENDAR_BUSY,
    CALENDAR_FREE,
    CALENDAR_OFF,
    CALENDAR_ARROW_BLANK,
    CALENDAR_FILL,
    calendar_title,
    calendar_keyboard,
)
from app.bot.states.booking_states import CheckTimeFSM
from app.database.models import BlockedPeriod, Service, WorkingHours
from app.utils.timezone import get_master_tz
from tests.conftest import MASTER_ID, RIGA_TZ, next_weekday
from tests.tg import tg_harness

CLIENT_TG = 111111


def _texts(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _button(markup, text: str):
    return next(button for row in markup.inline_keyboard for button in row if button.text == text)


def _cell(markup, day: dt.date):
    """Кнопка календаря, стоящая в сетке на месте указанного дня (после заголовка и строки дней недели)."""
    weeks = calendar.monthcalendar(day.year, day.month)
    week_index = next(i for i, week in enumerate(weeks) if day.day in week)
    return markup.inline_keyboard[2 + week_index][day.weekday()]


# ---------- клавиатура (без БД) ----------

TODAY = dt.date(2026, 10, 21)
LAST = TODAY + dt.timedelta(days=BOOKING_HORIZON_DAYS - 1)


def test_first_month_layout_shows_busy_and_off_days_as_symbols_without_numbers():
    states = {dt.date(2026, 10, 21): CALENDAR_FREE, dt.date(2026, 10, 22): CALENDAR_BUSY, dt.date(2026, 10, 23): CALENDAR_OFF}
    for day in range(24, 32):
        states[dt.date(2026, 10, day)] = CALENDAR_FREE
    markup = calendar_keyboard(2026, 10, today=TODAY, last_date=LAST, states=states)
    rows = _texts(markup)

    assert rows[0] == [CALENDAR_ARROW_BLANK, calendar_title(2026, 10), "▶️"]  # назад с первого месяца нельзя
    assert rows[1] == ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    assert rows[2] == [" ", " ", " ", "·", "·", "·", "·"]  # 1 октября — четверг
    assert rows[5] == ["·", "·", "[21]", "🔴", "➖", "24", "25"]
    assert rows[6] == ["26", "27", "28", "29", "30", "31", " "]
    assert rows[-1] == ["✖ Отмена"]
    assert _button(markup, "24").callback_data == DateCallback(date="2026-10-24").pack()
    assert _button(markup, "[21]").callback_data == DateCallback(date="2026-10-21").pack()
    assert _button(markup, "🔴").callback_data == CalendarCallback(action="busy").pack()
    assert _button(markup, "➖").callback_data == CalendarCallback(action="off").pack()
    assert _button(markup, "·").callback_data == CalendarCallback(action="noop").pack()
    assert _button(markup, "✖ Отмена").callback_data == CalendarCallback(action="cancel").pack()


def test_today_without_free_time_is_just_a_red_circle():
    markup = calendar_keyboard(2026, 10, today=TODAY, last_date=LAST, states={TODAY: CALENDAR_BUSY})
    assert _cell(markup, TODAY).text == "🔴"
    assert "[21]" not in [button.text for row in markup.inline_keyboard for button in row]


def test_today_off_is_just_a_dash():
    markup = calendar_keyboard(2026, 10, today=TODAY, last_date=LAST, states={TODAY: CALENDAR_OFF})
    assert _cell(markup, TODAY).text == "➖"


def test_middle_month_has_both_arrows():
    markup = calendar_keyboard(2026, 12, today=TODAY, last_date=LAST, states={})
    prev_button, title, next_button = markup.inline_keyboard[0]
    assert (prev_button.text, title.text, next_button.text) == ("◀️", calendar_title(2026, 12), "▶️")
    assert prev_button.callback_data == CalendarCallback(action="month", year=2026, month=11).pack()
    assert next_button.callback_data == CalendarCallback(action="month", year=2027, month=1).pack()


def test_title_has_same_length_for_every_month():
    # Telegram подбирает ширину блока по тексту кнопок: заголовок любого месяца обязан быть одной длины.
    lengths = {len(calendar_title(2027, month)) for month in range(1, 13)}
    assert len(lengths) == 1
    assert calendar_title(2027, 5).strip(CALENDAR_FILL) == "Май '27"


def test_calendar_text_keeps_header_and_adds_fixed_width_padding():
    # Ширина блока кнопок зависит от текста сообщения: у любого заголовка добавляется одна и та же строка-распорка.
    short, long = calendar_text("📅 Выберите дату"), calendar_text("Маникюр классический\n\n📅 Выберите дату:")
    assert short.startswith("📅 Выберите дату\n") and long.startswith("Маникюр классический\n\n📅 Выберите дату:\n")
    assert short.splitlines()[-1] == long.splitlines()[-1] and set(short.splitlines()[-1]) == {CALENDAR_FILL}


def test_year_boundary_arrows():
    markup = calendar_keyboard(2027, 1, today=TODAY, last_date=LAST, states={})
    assert markup.inline_keyboard[0][0].callback_data == CalendarCallback(action="month", year=2026, month=12).pack()


def test_last_month_has_no_next_arrow_and_days_after_horizon_are_blank():
    markup = calendar_keyboard(LAST.year, LAST.month, today=TODAY, last_date=LAST, states={})
    assert markup.inline_keyboard[0][2].text == CALENDAR_ARROW_BLANK
    assert _button(markup, str(LAST.day)).callback_data == DateCallback(date=LAST.isoformat()).pack()
    after = LAST + dt.timedelta(days=1)
    if after.month == LAST.month:
        assert str(after.day) not in [b.text for row in markup.inline_keyboard[2:-1] for b in row]


def test_horizon_is_six_months():
    assert BOOKING_HORIZON_DAYS == 180


# ---------- обработчики (БД) ----------


@pytest_asyncio.fixture
async def world(session, master_profile):
    """Пн–Сб рабочие 10–19, воскресенье — выходной; услуга 60 минут; среда через 30+ дней заблокирована целиком."""
    for weekday in range(6):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    session.add(Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0))
    blocked = next_weekday(2, 30)
    session.add(
        BlockedPeriod(
            master_id=MASTER_ID,
            start_datetime=dt.datetime.combine(blocked, dt.time(0, 0), tzinfo=RIGA_TZ),
            end_datetime=dt.datetime.combine(blocked + dt.timedelta(days=1), dt.time(0, 0), tzinfo=RIGA_TZ),
        )
    )
    await session.commit()
    return {"blocked": blocked, "sunday": next_weekday(6, 30), "free": next_weekday(1, 30)}


async def _open_month(h, day: dt.date):
    await h.press(CLIENT_TG, CalendarCallback(action="month", year=day.year, month=day.month))
    return [m for m in h.tg.requests if isinstance(m, EditMessageText)][-1].reply_markup


async def test_start_opens_calendar_of_current_month(session, world):
    tz = await get_master_tz(session, MASTER_ID)
    today = dt.datetime.now(tz).date()
    async with tg_harness(client_check_time.router) as h:
        await h.send_text(CLIENT_TG, "🕒 Проверить время")
        assert await h.get_state(CLIENT_TG) == CheckTimeFSM.select_date.state
        sent = [m for m in h.tg.requests if isinstance(m, SendMessage)][-1]
    assert sent.text == calendar_text("🕐 Выберите дату")
    assert sent.reply_markup.inline_keyboard[0][1].text == calendar_title(today.year, today.month)
    assert _button(sent.reply_markup, "✖ Отмена")


async def test_days_are_marked_free_busy_and_off(session, world):
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        blocked_month = await _open_month(h, world["blocked"])
        assert _cell(blocked_month, world["blocked"]).text == "🔴"
        sunday_month = await _open_month(h, world["sunday"])
        assert _cell(sunday_month, world["sunday"]).text == "➖"
        free_month = await _open_month(h, world["free"])
        assert _cell(free_month, world["free"]).callback_data == DateCallback(date=world["free"].isoformat()).pack()


async def test_today_without_room_for_the_shortest_service_is_busy(session, master_profile):
    tz = await get_master_tz(session, MASTER_ID)
    today = dt.datetime.now(tz).date()
    # Окно 30 минут при услуге в 60: сегодня занято при любом времени запуска теста.
    for weekday in range(7):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=True, start_time=dt.time(10), end_time=dt.time(10, 30)))
    session.add(Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0))
    await session.commit()
    async with tg_harness(client_check_time.router) as h:
        await h.send_text(CLIENT_TG, "🕒 Проверить время")
        sent = [m for m in h.tg.requests if isinstance(m, SendMessage)][-1]
    assert _cell(sent.reply_markup, today).text == "🔴"


async def test_today_off_is_marked_off(session, master_profile):
    tz = await get_master_tz(session, MASTER_ID)
    today = dt.datetime.now(tz).date()
    for weekday in range(7):
        session.add(
            WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=weekday != today.weekday(), start_time=dt.time(10), end_time=dt.time(19))
        )
    session.add(Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0))
    await session.commit()
    async with tg_harness(client_check_time.router) as h:
        await h.send_text(CLIENT_TG, "🕒 Проверить время")
        sent = [m for m in h.tg.requests if isinstance(m, SendMessage)][-1]
    assert _cell(sent.reply_markup, today).text == "➖"


async def test_busy_and_off_days_answer_with_popup(session, world):
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        await h.press(CLIENT_TG, CalendarCallback(action="busy"))
        await h.press(CLIENT_TG, CalendarCallback(action="off"))
        assert h.tg.alerts() == ["На этот день нет свободного времени", "Этот день выходной"]


async def test_inactive_cells_answer_silently(session, world):
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        await h.press(CLIENT_TG, CalendarCallback(action="noop"))
        answers = [m for m in h.tg.requests if isinstance(m, AnswerCallbackQuery)]
        assert len(answers) == 1 and not answers[0].text and not answers[0].show_alert
        assert not [m for m in h.tg.requests if isinstance(m, EditMessageText)]


async def test_month_navigation_is_clamped_to_booking_window(session, world):
    tz = await get_master_tz(session, MASTER_ID)
    today = dt.datetime.now(tz).date()
    last = today + dt.timedelta(days=BOOKING_HORIZON_DAYS - 1)
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        past = await _open_month(h, dt.date(2020, 1, 1))
        assert past.inline_keyboard[0][1].text == calendar_title(today.year, today.month)
        assert past.inline_keyboard[0][0].text == CALENDAR_ARROW_BLANK  # на текущем месяце ◀️ нет
        future = await _open_month(h, dt.date(2035, 1, 1))
        assert future.inline_keyboard[0][1].text == calendar_title(last.year, last.month)
        assert future.inline_keyboard[0][2].text == CALENDAR_ARROW_BLANK  # дальше горизонта ▶️ нет


async def test_forged_month_is_ignored(session, world):
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        await h.press(CLIENT_TG, CalendarCallback(action="month", year=0, month=13))
        assert not [m for m in h.tg.requests if isinstance(m, EditMessageText)]
        assert await h.get_state(CLIENT_TG) == CheckTimeFSM.select_date.state


async def test_cancel_clears_state_and_deletes_calendar(session, world):
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        await h.press(CLIENT_TG, CalendarCallback(action="cancel"))
        assert await h.get_state(CLIENT_TG) is None
        assert [m for m in h.tg.requests if isinstance(m, DeleteMessage)]


async def test_no_working_days_means_no_calendar(session, master_profile):
    async with tg_harness(client_check_time.router) as h:
        await h.send_text(CLIENT_TG, "🕒 Проверить время")
        assert h.tg.sent_to(CLIENT_TG) == ["Рабочие дни пока не настроены."]
        assert await h.get_state(CLIENT_TG) is None


async def test_free_day_opens_the_schedule_as_before(session, world):
    async with tg_harness(client_check_time.router) as h:
        await h.set_state(CLIENT_TG, CheckTimeFSM.select_date)
        await h.press(CLIENT_TG, DateCallback(date=world["free"].isoformat()))
        assert h.tg.sent_to(CLIENT_TG)[-1] == f"📅 {world['free'].strftime('%d.%m.%Y')}\nНа этот день пока нет записей."
