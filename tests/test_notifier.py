"""Уведомления мастеру уходят в режиме HTML: имя клиента и название услуги — пользовательский ввод и обязаны экранироваться."""

import datetime as dt
import re
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot

from app.database.models import Booking, User
from app.services.notifier import MasterNotifier
from tests.tg import BOT_ID, RecordingSession
from tests.conftest import MASTER_ID

ADMIN_ID = 999
TZ = ZoneInfo("Europe/Riga")
START = dt.datetime(2026, 11, 6, 15, 0, tzinfo=TZ)

HOSTILE_NAME = "<3 Anna <b>&</b>"
HOSTILE_SERVICE = "Брови & ресницы <i>"


@pytest.fixture
def notifier():
    tg = RecordingSession()
    return MasterNotifier(Bot(token=f"{BOT_ID}:TEST", session=tg), master_id=MASTER_ID, owner_telegram_id=ADMIN_ID), tg


def _booking(service_name: str = HOSTILE_SERVICE) -> Booking:
    return Booking(master_id=MASTER_ID,
        service_name_snapshot=service_name,
        service_price_snapshot=40,
        service_duration_snapshot=60,
        start_datetime=START,
        end_datetime=START + dt.timedelta(hours=1),
    )


def _client() -> User:
    return User(telegram_id=1, username="anna_k", first_name=HOSTILE_NAME)


def _assert_safe_html(text: str) -> None:
    """В тексте не должно остаться «сырых» угловых скобок и амперсандов — Telegram отвечает «can't parse entities»."""
    assert "<" not in text and ">" not in text
    assert not re.search(r"&(?!(?:amp|lt|gt|quot|#x?\d+);)", text)


async def test_new_booking_escapes_client_and_service(notifier):
    n, tg = notifier
    await n.notify_new_booking(_booking(), _client(), TZ)
    [text] = tg.sent_to(ADMIN_ID)
    _assert_safe_html(text)
    assert "&lt;3 Anna" in text and "Брови &amp; ресницы &lt;i&gt;" in text


async def test_cancelled_booking_escapes_client_and_service(notifier):
    n, tg = notifier
    await n.notify_cancelled_booking(_booking(), _client(), TZ)
    _assert_safe_html(tg.sent_to(ADMIN_ID)[0])


async def test_rescheduled_booking_escapes_client_and_service(notifier):
    n, tg = notifier
    new = _booking()
    new.start_datetime = START + dt.timedelta(days=1)
    new.end_datetime = new.start_datetime + dt.timedelta(hours=1)
    await n.notify_rescheduled_booking(_booking(), new, _client(), TZ)
    _assert_safe_html(tg.sent_to(ADMIN_ID)[0])


async def test_new_user_notification_still_escaped(notifier):
    n, tg = notifier
    await n.notify_new_user(_client())
    _assert_safe_html(tg.sent_to(ADMIN_ID)[0])


async def test_no_owner_sends_nothing(notifier):
    n, tg = notifier
    n.owner_telegram_id = None  # у мастера нет owner_telegram_id
    await n.notify_new_booking(_booking("Маникюр"), _client(), TZ)
    assert tg.requests == []


# --- «Мастер ограничил вам доступ» (клиенту) ---


async def test_client_restricted_with_cancelled_bookings_shows_price_and_split_date_time(notifier):
    n, tg = notifier
    await n.notify_client_restricted(_client(), [_booking("Маникюр")], TZ)
    [text] = tg.sent_to(1)
    assert text == (
        "🚫 Мастер ограничил вам доступ к записи, ваши записи отменены:\n\n"
        "Маникюр\n"
        "📅 06.11.2026\n"
        "🕐 15:00 - 16:00\n\n"
        "💰 40 BYN\n\n"
        "💬 Если у вас есть вопросы, вы можете связаться с мастером: "
        "откройте раздел «ℹ️ Информация» и нажмите «📞 Связаться со мной»."
    )


async def test_client_restricted_lists_every_cancelled_booking_with_its_own_price(notifier):
    n, tg = notifier
    second = _booking("Педикюр")
    second.service_price_snapshot = 55.5
    await n.notify_client_restricted(_client(), [_booking("Маникюр"), second], TZ)
    [text] = tg.sent_to(1)
    assert text.count("💰") == 2 and "💰 40 BYN" in text and "💰 55.50 BYN" in text
    assert text.index("Маникюр") < text.index("💰 40 BYN") < text.index("Педикюр") < text.index("💰 55.50 BYN")


async def test_client_restricted_escapes_service_name(notifier):
    n, tg = notifier
    await n.notify_client_restricted(_client(), [_booking()], TZ)
    [text] = tg.sent_to(1)
    _assert_safe_html(text)
    assert "Брови &amp; ресницы &lt;i&gt;" in text


async def test_client_restricted_without_bookings_tells_how_to_contact_master(notifier):
    n, tg = notifier
    await n.notify_client_restricted(_client(), [], TZ)
    assert tg.sent_to(1) == [
        "🚫 Мастер ограничил вам доступ: новые записи недоступны.\n\n"
        "Вы можете связаться с мастером, перейдя в раздел информация и отправить сообщение."
    ]


async def test_master_restricted_summary_keeps_its_own_format_without_price(notifier):
    """Правки касаются только сообщения клиенту: мастеру блоки записей приходят как раньше (дата и время в одной строке)."""
    n, tg = notifier
    await n.notify_master_client_restricted(_client(), [_booking("Маникюр")], TZ)
    [text] = tg.sent_to(ADMIN_ID)
    assert "Маникюр\n📅 06.11.2026  🕐 15:00 - 16:00" in text
    assert "💰" not in text

