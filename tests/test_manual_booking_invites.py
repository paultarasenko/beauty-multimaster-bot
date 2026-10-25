"""Запись, созданная мастером вручную: проверка контакта, приглашение клиенту и напоминания до привязки записи.

Три сценария, которые раньше вели себя неправильно:
1. контакт без «@» (или любой мусор) молча считался телефоном;
2. если клиент есть в базе бота, но написать ему не вышло (заблокировал бота), мастер не получал ссылку-приглашение;
3. пока клиент не открыл приглашение, «клиентские» напоминания уходили мастеру, а в напоминании мастеру вместо клиента
   стояло имя самого мастера.
"""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select

from app.bot.handlers.admin import add_booking
from app.bot.keyboards.callback_data import ConfirmCallback
from app.bot.states.booking_states import AdminAddBookingFSM
from app.database.models import Booking, User, WorkingHours
from app.database.session import make_session_factory
from app.scheduler.reminder_worker import (
    make_send_client_reminder_2h_func,
    make_send_master_reminder_2h_func,
    make_send_reminder_func,
)
from app.services.booking_service import BookingService
from app.utils.contact import normalize_contact
from tests.conftest import MASTER_ID, RIGA_TZ, TEST_DATABASE_URL, next_weekday, make_client, set_owner
from tests.tg import BOT_ID, RecordingSession, tg_harness

ADMIN_ID = 999
CLIENT_TG = 111111  # test_user, username "client1"
DAY = next_weekday(4)
START = dt.datetime.combine(DAY, dt.time(15, 0), tzinfo=RIGA_TZ)


# --- проверка контакта ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+375291234567", "+375291234567"),
        ("  +375 (29) 123-45-67  ", "+375 (29) 123-45-67"),
        ("80291234567", "80291234567"),
        ("@anna_k", "@anna_k"),
        ("  @Anna_K  ", "@Anna_K"),
        ("@" + "a" * 32, "@" + "a" * 32),
    ],
)
def test_valid_contacts_are_accepted(raw, expected):
    assert normalize_contact(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "anna_k",  # ник без «@» не угадываем: можно привязать запись к постороннему человеку
        "Анна",
        "@ann",  # короче 5 символов — такого username в Telegram не бывает
        "@" + "a" * 33,
        "@1anna",  # username не начинается с цифры
        "@anna-k",
        "@анна_к",
        "12345",  # слишком короткий номер
        "1" * 16,
        "+375 29 abc",
        "@anna_k @bob_b",
    ],
)
def test_invalid_contacts_are_rejected(raw):
    assert normalize_contact(raw) is None


# --- ввод контакта и приглашение при ручной записи ---


@pytest.fixture(autouse=True)
def _fake_bot_me(monkeypatch):
    async def me(self):
        return SimpleNamespace(username="test_bot")

    monkeypatch.setattr(Bot, "me", me)  # RecordingSession не умеет getMe, а ссылка-приглашение строится по нему


@pytest_asyncio.fixture
async def master_setup(session, master_profile, manicure_service):
    await set_owner(session, ADMIN_ID)
    session.add(WorkingHours(master_id=MASTER_ID, weekday=DAY.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    await session.commit()
    return manicure_service


async def _enter_contact(h, service, contact: str) -> None:
    await h.set_state(
        ADMIN_ID,
        AdminAddBookingFSM.enter_client_contact,
        client_name="Анна",
        service_id=service.id,
        selected_time=START.isoformat(),
    )
    await h.send_text(ADMIN_ID, contact)


async def _confirm(h, service, contact: str) -> None:
    await _enter_contact(h, service, contact)
    await h.press(ADMIN_ID, ConfirmCallback(action="yes"))


def _last_card(h) -> EditMessageText:
    return [m for m in h.tg.requests if isinstance(m, EditMessageText)][-1]


def _invite_buttons(card: EditMessageText) -> list:
    if card.reply_markup is None:
        return []
    return [b for row in card.reply_markup.inline_keyboard for b in row]


async def _only_booking(session) -> Booking:
    session.expire_all()
    return (await session.execute(select(Booking))).scalars().one()


async def test_invalid_contact_is_asked_again_and_booking_flow_does_not_advance(master_setup):
    async with tg_harness(add_booking.router) as h:
        await _enter_contact(h, master_setup, "anna_k")

        assert await h.get_state(ADMIN_ID) == AdminAddBookingFSM.enter_client_contact.state
        assert any("Не похоже на контакт" in t for t in h.tg.sent_to(ADMIN_ID))
        assert not any("Проверьте запись" in t for t in h.tg.sent_to(ADMIN_ID))


async def test_valid_contact_moves_to_confirmation(master_setup):
    async with tg_harness(add_booking.router) as h:
        await _enter_contact(h, master_setup, "  @client1 ")

        assert await h.get_state(ADMIN_ID) == AdminAddBookingFSM.confirm.state
        card = next(t for t in h.tg.sent_to(ADMIN_ID) if "Проверьте запись" in t)
        assert "(@client1)" in card


async def test_known_client_who_blocked_the_bot_still_gets_an_invite_link(session, master_setup, test_user):
    client_id = test_user.id  # expire_all() в _only_booking сбрасывает атрибуты объекта
    async with tg_harness(add_booking.router) as h:
        h.tg.blocked_chat_ids.add(CLIENT_TG)
        await _confirm(h, master_setup, "@client1")

        booking = await _only_booking(session)
        assert booking.user_id == client_id  # запись уже привязана к самому клиенту
        assert booking.invite_token

        card = _last_card(h)
        assert "заблокировал бота" in card.text
        [button] = _invite_buttons(card)
        assert button.text == "📨 Отправить клиенту"
        assert f"start%3Dinv_{booking.invite_token}" in button.url


async def test_known_client_who_can_receive_messages_is_notified_without_invite(session, master_setup, test_user):
    client_id = test_user.id
    async with tg_harness(add_booking.router) as h:
        await _confirm(h, master_setup, "@client1")

        booking = await _only_booking(session)
        assert booking.user_id == client_id
        assert booking.invite_token is None
        assert any("Мастер создал для вас запись" in t for t in h.tg.sent_to(CLIENT_TG))
        card = _last_card(h)
        assert "клиент уведомлен" in card.text
        assert _invite_buttons(card) == []


async def test_unknown_client_gets_invite_and_booking_stays_with_master(session, master_setup):
    async with tg_harness(add_booking.router) as h:
        await _confirm(h, master_setup, "+375291234567")

        booking = await _only_booking(session)
        owner = await session.get(User, booking.user_id)
        assert owner.telegram_id == ADMIN_ID
        assert booking.invite_token
        card = _last_card(h)
        assert "Клиента ещё нет в боте" in card.text
        assert len(_invite_buttons(card)) == 1


async def test_invite_for_blocked_client_can_be_claimed_again(session, master_setup, test_user):
    """Клиент вернулся и открыл ссылку: запись остаётся за ним, токен гасится (ссылка одноразовая)."""
    client_id = test_user.id
    async with tg_harness(add_booking.router) as h:
        h.tg.blocked_chat_ids.add(CLIENT_TG)
        await _confirm(h, master_setup, "@client1")
    booking = await _only_booking(session)
    token = booking.invite_token

    claimed = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).claim_invitation(
        token, client_id, now=START - dt.timedelta(days=1)
    )
    await session.commit()

    assert claimed is not None and claimed.user_id == client_id and claimed.invite_token is None


# --- напоминания по записи, которую клиент ещё не принял ---


@pytest.fixture
def tg_bot():
    tg = RecordingSession()
    return Bot(token=f"{BOT_ID}:TEST", session=tg), tg


@pytest.fixture
def session_factory():
    return make_session_factory(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def unclaimed_booking(session, master_setup) -> Booking:
    """Запись, созданная мастером для человека, которого нет в боте: числится за мастером."""
    master = await make_client(session, ADMIN_ID, username="master_test", first_name="Мастер")
    await session.commit()
    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(
        master.id,
        master_setup.id,
        START,
        manual_client_name="Анна <3",
        manual_client_contact="+375291234567",
    )
    booking.invite_token = "tok123"
    await session.commit()
    return booking


def _sent(tg: RecordingSession) -> list[SendMessage]:
    return [m for m in tg.requests if isinstance(m, SendMessage)]


async def test_client_reminders_are_not_sent_to_master_before_invite_is_opened(
    unclaimed_booking, tg_bot, session_factory
):
    bot, tg = tg_bot
    await make_send_reminder_func(bot, session_factory, MASTER_ID)(unclaimed_booking)
    await make_send_client_reminder_2h_func(bot, session_factory, MASTER_ID)(unclaimed_booking)

    assert _sent(tg) == []


async def test_master_reminder_names_the_client_entered_by_master(unclaimed_booking, tg_bot, session_factory):
    bot, tg = tg_bot
    await make_send_master_reminder_2h_func(bot, session_factory, MASTER_ID)(unclaimed_booking)

    [msg] = _sent(tg)
    assert msg.chat_id == ADMIN_ID
    assert "Анна &lt;3 (+375291234567)" in msg.text
    assert "Мастер" not in msg.text and "master_test" not in msg.text


async def test_reminders_go_to_client_after_invite_is_opened(
    session, unclaimed_booking, test_user, tg_bot, session_factory
):
    claimed = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).claim_invitation(
        "tok123", test_user.id, now=START - dt.timedelta(days=1)
    )
    await session.commit()
    assert claimed is not None

    bot, tg = tg_bot
    await make_send_reminder_func(bot, session_factory, MASTER_ID)(claimed)
    await make_send_client_reminder_2h_func(bot, session_factory, MASTER_ID)(claimed)
    await make_send_master_reminder_2h_func(bot, session_factory, MASTER_ID)(claimed)

    to_client = [m for m in _sent(tg) if m.chat_id == CLIENT_TG]
    to_master = [m for m in _sent(tg) if m.chat_id == ADMIN_ID]
    assert len(to_client) == 2
    assert len(to_master) == 1 and "@client1" in to_master[0].text
