import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot.handlers.admin.bookings import _format_booking_block

TZ = ZoneInfo("Europe/Minsk")


def _booking(start, minutes, name="🤍 Французский маникюр", price=95, manual=None, contact=None):
    return SimpleNamespace(
        start_datetime=start,
        end_datetime=start + dt.timedelta(minutes=minutes),
        service_name_snapshot=name,
        service_price_snapshot=price,
        manual_client_name=manual,
        manual_client_contact=contact,
    )


def test_booking_block_format():
    start = dt.datetime(2026, 10, 6, 17, 0, tzinfo=TZ)  # вторник
    user = SimpleNamespace(first_name="Clientusername_one", last_name=None, username="clientusername_one")
    text = _format_booking_block(_booking(start, 120), user, TZ, today=dt.date(2026, 10, 1))
    assert text == (
        "📅 06.10 · Вторник · 🕔 17:00 - 19:00\n"
        "🤍 Французский маникюр · 95 BYN\n"
        "👤 Clientusername_one · @clientusername_one"
    )


def test_booking_block_tomorrow_and_utc_input():
    start = dt.datetime(2026, 10, 2, 5, 0, tzinfo=dt.timezone.utc)  # 08:00 по Минску
    user = SimpleNamespace(first_name="Аня", last_name=None, username=None)
    text = _format_booking_block(_booking(start, 90), user, TZ, today=dt.date(2026, 10, 1))
    assert text.splitlines()[0] == "📅 02.10 · Завтра · 🕗 08:00 - 09:30"
