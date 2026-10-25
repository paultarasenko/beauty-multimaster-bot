"""Этап 12B: лёгкие проверки нагрузочного стенда (tools/loadtest). Без БД, сети и Docker."""

import random

import pytest

from app.bot.keyboards.callback_data import BookingActionCallback, DateCallback, ServiceCallback, TimeCallback
from app.bot.keyboards.client_keyboards import cancel_confirm_keyboard, dates_keyboard, main_menu_keyboard, my_booking_keyboard, services_keyboard
from tools.loadtest import report, updates
from tools.loadtest.common import GuardError, asyncpg_dsn, percentile, require_loadtest_db_url


def _markup(keyboard) -> dict:
    return keyboard.model_dump(exclude_none=True)


def test_updates_are_built_from_real_callback_data_classes():
    """Кнопки настоящих клавиатур разбираются классами CallbackData, а тексты меню нагрузки совпадают с настоящим меню."""
    import datetime as dt

    from app.database.models import Service

    rng = random.Random(1)
    menu_texts = {button.text for row in main_menu_keyboard(False).keyboard for button in row}
    assert set(updates.MENU_TEXTS) <= menu_texts and updates.BOOK_TEXT in menu_texts and updates.MY_BOOKINGS_TEXT in menu_texts

    service = Service(id=42, master_id=1, name="Маникюр", price=10, duration_minutes=60, buffer_minutes=0, active=True, sort_order=0)
    picked = updates.pick_callback(_markup(services_keyboard([service])), ServiceCallback, rng)
    assert picked == ServiceCallback(service_id=42)
    dates = dates_keyboard([dt.date(2030, 1, 7), dt.date(2030, 1, 8)])
    assert {cb.date for cb in updates.find_callbacks(_markup(dates), DateCallback)} == {"2030-01-07", "2030-01-08"}
    assert updates.find_callbacks(_markup(dates), TimeCallback) == []  # чужие кнопки пропускаются
    cancel = updates.pick_callback(_markup(my_booking_keyboard(7)), BookingActionCallback, rng, lambda cb: cb.action == "cancel")
    assert cancel == BookingActionCallback(booking_id=7, action="cancel")
    confirm = updates.pick_callback(_markup(cancel_confirm_keyboard(7)), BookingActionCallback, rng, lambda cb: cb.action == "cancel_confirm")

    update = updates.callback_update(5, 910000000001, confirm.pack(), 99, "cb1")
    assert BookingActionCallback.unpack(update["callback_query"]["data"]) == confirm
    command = updates.message_update(6, 910000000001, "/start", 1)["message"]
    assert command["entities"] == [{"type": "bot_command", "offset": 0, "length": 6}]


def test_percentiles_and_criteria():
    assert percentile([], 95) is None
    assert percentile([10], 99) == 10
    values = list(range(1, 101))
    assert (percentile(values, 50), percentile(values, 95), percentile(values, 99), percentile(values, 100)) == (50, 95, 99, 100)
    with pytest.raises(ValueError):
        percentile(values, 101)
    # «не хуже чем в 2 раза»; разница меньше 25 мс — шум
    assert report.degradation_ok(100, 200) and not report.degradation_ok(100, 201)
    assert report.degradation_ok(10, 30) and not report.degradation_ok(None, 5)

    class S:  # минимальный замер
        def __init__(self, latency, status=200):
            self.t, self.latency_ms, self.lag_ms, self.status = 0.0, latency, 0.0, status

    good = report.summarize([S(100)] * 19 + [S(400)])
    bad = report.summarize([S(100)] * 18 + [S(900), S(900, status=0)])
    verdict = lambda s: {c.name: c.status for c in report.evaluate("normal", {"summaries": {"main": s}, "ready_after": 200})}
    assert verdict(good) == {"Ошибок нет": "PASS", "p95 ответа webhook": "PASS", "Приложение живо после прогона (/ready)": "PASS"}
    assert verdict(bad)["Ошибок нет"] == "FAIL" and verdict(bad)["p95 ответа webhook"] == "FAIL"


def test_database_guard_requires_loadtest_name(monkeypatch):
    monkeypatch.delenv("LOADTEST_DATABASE_URL", raising=False)
    with pytest.raises(GuardError):  # переменная не задана: DATABASE_URL не используется
        require_loadtest_db_url()
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/loadtest")
    with pytest.raises(GuardError):
        require_loadtest_db_url()
    for name in ("beauty_bot", "beauty_bot_test", "my_loadtest", ""):
        with pytest.raises(GuardError) as excinfo:
            require_loadtest_db_url(f"postgresql+asyncpg://u:p@h:5432/{name}")
        assert excinfo.value.code == 2
    assert require_loadtest_db_url("postgresql+asyncpg://u:p@h:5432/loadtest").endswith("/loadtest")
    assert require_loadtest_db_url("postgresql://u:p@h:5432/loadtest_2").startswith("postgresql+asyncpg://")
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/loadtest").startswith("postgresql://u:p@h")
