"""Телефон клиента: бот один раз предлагает поделиться им после первой записи, передумать можно в «Мои записи».
Номер попадает в Excel-отчёт.

Настоящий диспетчер и БД, Telegram — запись запросов (см. tests/tg.py).
"""

import datetime as dt

import pytest
import pytest_asyncio
from aiogram.types import Chat, Contact, Message, Update
from sqlalchemy import select

from app.bot.handlers.admin import master_settings as admin_settings
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import my_booking as client_my_booking
from app.bot.keyboards.callback_data import ConfirmCallback, PhoneCallback
from app.bot.keyboards.client_keyboards import CALENDAR_ARROW_BLANK
from app.bot.states.booking_states import BookingFSM
from app.database.models import Booking, Master, WorkingHours, Service
from app.database.repositories.master_settings_repository import MasterSettingsRepository
from app.database.repositories.user_repository import UserRepository
from app.utils.phone import normalize_phone
from tests.conftest import MASTER_ID, RIGA_TZ, client_row, make_client, next_weekday, set_client
from tests.test_report_service import add_booking, col, local, report
from tests.test_stage8_settings import ADMIN, BTN_SETTINGS, cb, set_owner, settings_of
from tests.tg import tg_harness

CLIENT_TG = 111111  # test_user
OTHER_TG = 222222  # test_user_2
SECOND_MASTER_ID = 2


@pytest_asyncio.fixture
async def world(session, master_profile, test_user):
    """Рабочий день через 2+ недели и услуга. Записей ещё нет. Запрос номера включён (по умолчанию он выключен)."""
    await MasterSettingsRepository(session, MASTER_ID).update_fields(ask_phone_enabled=True)
    day = next_weekday(4)
    session.add(WorkingHours(master_id=MASTER_ID, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    service = Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)
    await session.commit()

    def at(hour: int) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(hour, 0), tzinfo=RIGA_TZ)

    return {"at": at, "service_id": service.id}


async def _book(h, world, user_id=CLIENT_TG, hour=17):
    """Клиент дошёл до экрана «Проверьте запись» и нажал «Подтвердить»."""
    await h.set_state(user_id, BookingFSM.confirm, service_id=world["service_id"], selected_time=world["at"](hour).isoformat())
    await h.press(user_id, ConfirmCallback(action="yes"))


async def _share_contact(h, user_id: int, phone: str, contact_user_id: int | None) -> None:
    """Клиент нажал «Да, поделиться номером»: Telegram присылает сообщение с контактом."""
    h._update_id += 1
    message = Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=h._user(user_id),
        contact=Contact(phone_number=phone, first_name="Ivan", user_id=contact_user_id),
    )
    await h.dp.feed_update(h.bot, Update(update_id=h._update_id, message=message))


def _offers(h, user_id=CLIENT_TG) -> list:
    """Отправленные клиенту сообщения с предложением номера."""
    return [
        m for m in h.tg.requests
        if getattr(m, "chat_id", None) == user_id and getattr(m, "text", None) == client_booking.PHONE_OFFER_TEXT
    ]


def _has_contact_button(markup) -> bool:
    return any(button.request_contact for row in markup.keyboard for button in row)


async def _bookings(session) -> list[Booking]:
    session.expire_all()
    return list((await session.execute(select(Booking).order_by(Booking.id))).scalars())


# --- нормализация номера ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("375291234567", "+375291234567"),
        ("+375291234567", "+375291234567"),
        ("+375 (29) 123-45-67", "+375291234567"),
        ("123", None),
        ("+1234567890123456", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


# --- предложение после первой записи ---


async def test_first_booking_is_created_and_phone_is_offered_afterwards(session, world, test_user):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _book(h, world)

        offers = _offers(h)
        assert len(offers) == 1
        assert _has_contact_button(offers[0].reply_markup), "кнопка «Да» — обычная (reply): у инлайн-кнопок request_contact нет"
        assert await h.get_state(CLIENT_TG) == BookingFSM.share_phone.state
    assert (await client_row(session, test_user)).phone_offered_at is not None
    assert len(await _bookings(session)) == 1, "запись создаётся сразу, не дожидаясь ответа про номер"


async def test_sharing_own_contact_saves_phone_and_returns_the_menu(session, world, test_user):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _book(h, world)
        await _share_contact(h, CLIENT_TG, "375291234567", contact_user_id=CLIENT_TG)

        assert await h.get_state(CLIENT_TG) is None
        assert client_booking.PHONE_SAVED_TEXT in h.tg.sent_to(CLIENT_TG)
    assert (await client_row(session, test_user)).phone == "+375291234567"


async def test_someone_elses_contact_is_rejected(session, world, test_user):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _book(h, world)
        await _share_contact(h, CLIENT_TG, "375299999999", contact_user_id=OTHER_TG)

        assert await h.get_state(CLIENT_TG) == BookingFSM.share_phone.state
        assert client_booking.PHONE_NOT_YOURS_TEXT in h.tg.sent_to(CLIENT_TG)
    assert (await client_row(session, test_user)).phone is None


async def test_not_now_declines_and_the_bot_does_not_ask_again(session, world, test_user):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _book(h, world, hour=15)
        await h.send_text(CLIENT_TG, "Не сейчас")

        assert await h.get_state(CLIENT_TG) is None
        assert client_booking.PHONE_DECLINED_TEXT in h.tg.sent_to(CLIENT_TG)

        await _book(h, world, hour=17)  # вторая запись: больше не предлагаем

        assert len(_offers(h)) == 1
        assert await h.get_state(CLIENT_TG) is None
    assert (await client_row(session, test_user)).phone is None
    assert len(await _bookings(session)) == 2


async def test_no_offer_when_phone_is_already_saved(session, world, test_user):
    await set_client(session, test_user, phone="+375291234567")
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _book(h, world)

        assert _offers(h) == []
        assert await h.get_state(CLIENT_TG) is None
    assert len(await _bookings(session)) == 1


async def test_text_instead_of_contact_repeats_the_offer(session, world):
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _book(h, world)
        await h.send_text(CLIENT_TG, "+375291234567")  # набрал руками: номер не принимается

        assert await h.get_state(CLIENT_TG) == BookingFSM.share_phone.state
        assert len(_offers(h)) == 2


async def test_command_during_the_offer_resets_the_state(session, world):
    async with tg_harness(client_booking.router, error_middleware=True, reset_input=True) as h:
        await _book(h, world)
        await h.send_text(CLIENT_TG, "/start")

        assert await h.get_state(CLIENT_TG) is None


# --- «Мои записи»: можно передумать ---


async def _my_bookings_world(session, world, test_user, **client_flags):
    service = (await session.execute(select(Service))).scalars().first()
    await add_booking(session, test_user, service, world["at"](17))
    if client_flags:
        await set_client(session, test_user, **client_flags)


def _callback_buttons(h, user_id=CLIENT_TG) -> list[str]:
    """callback_data всех инлайн-кнопок, отправленных клиенту."""
    result = []
    for m in h.tg.requests:
        markup = getattr(m, "reply_markup", None)
        if getattr(m, "chat_id", None) == user_id and hasattr(markup, "inline_keyboard"):
            result += [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    return result


async def test_my_bookings_shows_share_phone_button_until_phone_is_saved(session, world, test_user):
    await _my_bookings_world(session, world, test_user)
    async with tg_harness(client_my_booking.router, error_middleware=True) as h:
        await h.send_text(CLIENT_TG, "📋 Мои записи")

        assert PhoneCallback(action="share").pack() in _callback_buttons(h)
        assert client_my_booking.PHONE_HINT_TEXT in h.tg.sent_to(CLIENT_TG)


async def test_my_bookings_has_no_share_phone_button_when_phone_is_saved(session, world, test_user):
    await _my_bookings_world(session, world, test_user, phone="+375291234567")
    async with tg_harness(client_my_booking.router, error_middleware=True) as h:
        await h.send_text(CLIENT_TG, "📋 Мои записи")

        assert PhoneCallback(action="share").pack() not in _callback_buttons(h)
        assert client_my_booking.PHONE_HINT_TEXT not in h.tg.sent_to(CLIENT_TG)


async def test_share_phone_button_works_even_after_declining(session, world, test_user):
    """Отказался после первой записи, потом передумал: кнопка в «Мои записи» -> «Да» -> номер сохранён."""
    await _my_bookings_world(session, world, test_user, phone_offered_at=dt.datetime.now(dt.timezone.utc))
    async with tg_harness(client_booking.router, client_my_booking.router, error_middleware=True) as h:
        await h.send_text(CLIENT_TG, "📋 Мои записи")
        assert PhoneCallback(action="share").pack() in _callback_buttons(h)

        await h.press(CLIENT_TG, PhoneCallback(action="share"))
        assert await h.get_state(CLIENT_TG) == BookingFSM.share_phone.state
        assert len(_offers(h)) == 1

        await _share_contact(h, CLIENT_TG, "375291234567", contact_user_id=CLIENT_TG)
    assert (await client_row(session, test_user)).phone == "+375291234567"


async def test_share_phone_button_when_phone_is_already_saved_says_so(session, world, test_user):
    await set_client(session, test_user, phone="+375291234567")
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await h.press(CLIENT_TG, PhoneCallback(action="share"))

        assert "Номер уже сохранён ✅" in h.tg.alerts()
        assert await h.get_state(CLIENT_TG) is None


# --- мастер выключил запрос (так по умолчанию) ---


async def test_nothing_is_offered_when_the_master_has_not_enabled_the_request(session, world, test_user):
    await MasterSettingsRepository(session, MASTER_ID).update_fields(ask_phone_enabled=False)
    await session.commit()
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _book(h, world)

        assert _offers(h) == []
        assert await h.get_state(CLIENT_TG) is None
    assert (await client_row(session, test_user)).phone_offered_at is None
    assert len(await _bookings(session)) == 1


async def test_my_bookings_has_no_share_phone_button_when_the_request_is_disabled(session, world, test_user):
    await MasterSettingsRepository(session, MASTER_ID).update_fields(ask_phone_enabled=False)
    await session.commit()
    await _my_bookings_world(session, world, test_user)
    async with tg_harness(client_my_booking.router, error_middleware=True) as h:
        await h.send_text(CLIENT_TG, "📋 Мои записи")

        assert PhoneCallback(action="share").pack() not in _callback_buttons(h)


async def test_old_share_phone_button_does_nothing_when_the_request_is_disabled(session, world, test_user):
    await MasterSettingsRepository(session, MASTER_ID).update_fields(ask_phone_enabled=False)
    await session.commit()
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await h.press(CLIENT_TG, PhoneCallback(action="share"))

        assert "Сейчас номер не нужен." in h.tg.alerts()
        assert await h.get_state(CLIENT_TG) is None


# --- «⚙️ Настройки» мастера: две страницы, переключатель запроса номера ---


def _last_rows(h) -> list[list[str]]:
    """Подписи кнопок последнего экрана с инлайн-клавиатурой."""
    markups = [m.reply_markup for m in h.tg.requests if hasattr(getattr(m, "reply_markup", None), "inline_keyboard")]
    return [[b.text for b in row] for row in markups[-1].inline_keyboard]


def _last_buttons(h) -> list:
    markups = [m.reply_markup for m in h.tg.requests if hasattr(getattr(m, "reply_markup", None), "inline_keyboard")]
    return [b for row in markups[-1].inline_keyboard for b in row]


async def test_settings_first_page_has_five_buttons_without_reschedule_and_next_arrow(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.send_text(ADMIN, BTN_SETTINGS)
        rows = _last_rows(h)

    assert len(rows) == 6  # 5 кнопок + ряд листания
    assert [r[0].split(":")[0] for r in rows[:5]] == [
        "🕐 Интервал записи", "🔔 Напоминания", "🌙 Тихий режим", "📋 Лимиты записей", "⭐ Отзывы клиентов",
    ]
    assert not any("Перенос" in text for row in rows for text in row)
    assert rows[5] == [CALENDAR_ARROW_BLANK, "1/2", "▶️"]


async def test_settings_second_page_has_phone_toggle_and_back_arrow(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.send_text(ADMIN, BTN_SETTINGS)
        await h.press(ADMIN, cb("menu", "main2"))
        rows = _last_rows(h)

    assert [r[0].split(":")[0] for r in rows[:5]] == [
        "📱 Запрос сотового номера", "💱 Валюта", "✍️ Тексты сообщений", "🖼 Фото профиля", "🤖 Мой бот",
    ]
    assert rows[0] == ["📱 Запрос сотового номера: ⚪ выкл"]  # по умолчанию выключен
    assert rows[5] == ["◀️", "2/2", CALENDAR_ARROW_BLANK]


async def test_settings_arrows_page_back_and_forth(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.send_text(ADMIN, BTN_SETTINGS)
        await h.press(ADMIN, cb("menu", "main2"))
        await h.press(ADMIN, cb("menu", "main"))
        assert _last_rows(h)[0][0].startswith("🕐 Интервал записи")


async def test_phone_toggle_switches_the_setting_and_stays_on_the_second_page(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.send_text(ADMIN, BTN_SETTINGS)
        await h.press(ADMIN, cb("menu", "main2"))
        await h.press(ADMIN, cb("toggle", "ask_phone_enabled", "main2"))

        assert (await settings_of(session)).ask_phone_enabled is True
        assert _last_rows(h)[0] == ["📱 Запрос сотового номера: 🟢 вкл"], "после переключения остаёмся на второй странице"
        assert _last_rows(h)[-1] == ["◀️", "2/2", CALENDAR_ARROW_BLANK]
        assert any("📱 Запрос сотового номера: 🟢 вкл" in t for t in h.tg.edited())

        await h.press(ADMIN, cb("toggle", "ask_phone_enabled", "main2"))
        assert (await settings_of(session)).ask_phone_enabled is False
        assert _last_rows(h)[0] == ["📱 Запрос сотового номера: ⚪ выкл"]


async def test_blank_arrow_and_page_label_do_nothing(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.send_text(ADMIN, BTN_SETTINGS)
        before = len(h.tg.edited())
        await h.press(ADMIN, cb("noop"))

        assert len(h.tg.edited()) == before
    assert (await settings_of(session)).ask_phone_enabled is False


async def test_back_from_second_page_sections_returns_to_the_second_page(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        for open_action in (cb("currency"), cb("menu", "texts"), cb("photo")):
            await h.press(ADMIN, open_action)
            back = [b for b in _last_buttons(h) if b.text == "⬅️ Назад"]
            assert back and back[0].callback_data == cb("menu", "main2").pack(), open_action


# --- приватность ---


async def test_phone_is_private_to_the_master(session, test_user):
    """Номер, оставленный одному мастеру, другой мастер не видит."""
    session.add(Master(id=SECOND_MASTER_ID, owner_telegram_id=999))
    await session.flush()
    await make_client(session, test_user.telegram_id, master_id=SECOND_MASTER_ID)
    await set_client(session, test_user, phone="+375291234567")

    assert await UserRepository(session, MASTER_ID).get_phone(test_user.id) == "+375291234567"
    assert await UserRepository(session, SECOND_MASTER_ID).get_phone(test_user.id) is None


# --- Excel-отчёт ---


async def test_report_has_phone_column_between_contact_and_created(session, master_profile, test_user, manicure_service):
    await add_booking(session, test_user, manicure_service, local(12))
    _, header, _, _, _ = await report(session)

    names = list(header)
    assert names[names.index("Контакт") + 1] == "Телефон"
    assert names[names.index("Телефон") + 1] == "Создана"


async def test_report_shows_phones_of_bot_clients_and_manual_clients(
    session, master_profile, test_user, test_user_2, manicure_service
):
    await set_client(session, test_user, phone="+375291234567")
    await add_booking(session, test_user, manicure_service, local(10))  # клиент поделился номером
    await add_booking(session, test_user_2, manicure_service, local(12))  # номера нет
    await add_booking(session, test_user, manicure_service, local(14), manual_name="Анна", manual_contact="+375 29 111 22 33")
    await add_booking(session, test_user, manicure_service, local(16), manual_name="Ольга", manual_contact="@olga_k")

    ws, _, body, totals, _ = await report(session)

    assert col(body, "Телефон") == ["+375291234567", "—", "+375 29 111 22 33", "—"]
    assert all(len(row) == 12 for row in body + totals), "в итоговых строках должно быть столько же ячеек, сколько колонок"
