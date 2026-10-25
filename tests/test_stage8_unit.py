"""Этап 8, чистые тесты без БД: границы настроек, шаблоны текстов, «зашитые» значения в коде."""

import re
from pathlib import Path

import pytest

from app.utils import master_settings as ms
from app.utils.money import format_money, format_price
from app.utils.text_templates import (
    ALLOWED_FIELDS,
    EXAMPLE_VALUES,
    MAX_RENDERED_LEN,
    TemplateError,
    render_preview,
    render_template,
    validate_template,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# ---------- значения по умолчанию = прежнее поведение ----------


def test_defaults_equal_pre_stage8_behaviour():
    d = ms.MasterSettingsData()
    assert (d.slot_interval_minutes, d.max_active_bookings, d.cancel_streak_limit) == (30, 3, 3)
    assert d.currency == "BYN"
    assert d.reminder_24h_enabled and d.reminder_2h_enabled and d.master_reminder_2h_enabled
    assert d.allow_reschedule and d.allow_reviews
    assert all(getattr(d, f) is None for f in ms.TEXT_FIELDS)
    assert d.disabled_notification_types() == frozenset()


def test_service_module_constants_keep_old_values():
    from app.services.booking_service import CANCEL_STREAK_ALERT, MAX_ACTIVE_BOOKINGS

    assert (MAX_ACTIVE_BOOKINGS, CANCEL_STREAK_ALERT) == (3, 3)


def test_disabled_notification_types_map_to_notification_enum():
    from app.database.models import NotificationType

    d = ms.MasterSettingsData(reminder_24h_enabled=False, reminder_2h_enabled=False, master_reminder_2h_enabled=False)
    assert d.disabled_notification_types() == {t.value for t in NotificationType}
    only_master = ms.MasterSettingsData(master_reminder_2h_enabled=False)
    assert only_master.disabled_notification_types() == {NotificationType.REMINDER_2H_MASTER.value}


def test_setting_fields_cover_the_model():
    from app.database.models import MasterSettings

    columns = {c.name for c in MasterSettings.__table__.columns} - {"id", "master_id", "created_at", "updated_at"}
    assert columns == set(ms.SETTING_FIELDS)


# ---------- проверка значений ----------


@pytest.mark.parametrize("value", [15, 30, 60])
def test_slot_interval_accepts_allowed(value):
    assert ms.validate_slot_interval(value) == value


@pytest.mark.parametrize("value", [0, 10, 20, 45, 90, -30, True, "30", None, 30.0])
def test_slot_interval_rejects_everything_else(value):
    with pytest.raises(ms.SettingError):
        ms.validate_slot_interval(value)


@pytest.mark.parametrize("value", [1, 3, 10])
def test_max_active_bookings_bounds_ok(value):
    assert ms.validate_max_active_bookings(value) == value


@pytest.mark.parametrize("value", [0, -1, 11, 100, True, "3", None])
def test_max_active_bookings_bounds_rejected(value):
    with pytest.raises(ms.SettingError):
        ms.validate_max_active_bookings(value)


@pytest.mark.parametrize("value", [2, 3, 10])
def test_cancel_streak_bounds_ok(value):
    assert ms.validate_cancel_streak_limit(value) == value


@pytest.mark.parametrize("value", [0, 1, 11, -3, True, "3"])
def test_cancel_streak_bounds_rejected(value):
    with pytest.raises(ms.SettingError):
        ms.validate_cancel_streak_limit(value)


@pytest.mark.parametrize("value,expected", [("BYN", "BYN"), (" rub ", "rub"), ("₽", "₽"), ("€", "€"), ("KZT", "KZT"), ("12345678", "12345678")])
def test_currency_ok(value, expected):
    assert ms.validate_currency(value) == expected


@pytest.mark.parametrize("value", ["", "   ", None, 5, "TOOLONGCUR", "B Y", "<b>", "A&B", "{x}", "BY\nN", "a\x00", "😀" * 9])
def test_currency_rejected(value):
    with pytest.raises(ms.SettingError):
        ms.validate_currency(value)


@pytest.mark.parametrize("text", ["12", " 7 ", "0"])
def test_parse_int_ok(text):
    assert ms.parse_int_input(text) == int(text)


@pytest.mark.parametrize("text", ["", None, "-1", "1.5", "abc", "١٢", "12345", "1 2"])
def test_parse_int_rejected(text):
    with pytest.raises(ms.SettingError):
        ms.parse_int_input(text)


def test_validate_setting_rejects_unknown_field_and_non_bool():
    with pytest.raises(ms.SettingError):
        ms.validate_setting("master_id", 5)
    with pytest.raises(ms.SettingError):
        ms.validate_setting("allow_reviews", "yes")
    assert ms.validate_setting("allow_reviews", False) is False


def test_default_value_unknown_field():
    with pytest.raises(KeyError):
        ms.default_value("nope")
    assert ms.default_value("currency") == "BYN"
    assert ms.default_value("reminder_2h_text") is None


@pytest.mark.parametrize(
    "d,expected",
    [
        (ms.MasterSettingsData(), "за 24 и за 2 часа"),
        (ms.MasterSettingsData(reminder_2h_enabled=False), "за 24 часа"),
        (ms.MasterSettingsData(reminder_24h_enabled=False), "за 2 часа"),
        (ms.MasterSettingsData(reminder_24h_enabled=False, reminder_2h_enabled=False), None),
    ],
)
def test_client_reminder_phrase(d, expected):
    assert ms.client_reminder_phrase(d) == expected


def test_default_phrase_matches_old_text():
    assert f"Напомним о ней {ms.client_reminder_phrase(ms.MasterSettingsData())} 🙂" == "Напомним о ней за 24 и за 2 часа 🙂"


def test_count_genitive():
    assert ms.count_genitive(3) == "трёх" and ms.count_genitive(1) == "одной" and ms.count_genitive(99) == "99"


def test_over_limit_headline_default_text_unchanged_and_follows_limit():
    from app.services.notifier import over_limit_cancellation_headline

    assert over_limit_cancellation_headline(3).endswith("хотя у него было больше трёх записей!")
    assert "больше пяти записей" in over_limit_cancellation_headline(3, 5)


def test_limit_reached_text_follows_limit_and_default_unchanged():
    from app.bot.handlers.client.booking import limit_reached_text

    assert limit_reached_text() == limit_reached_text(None, 0, 3)
    assert limit_reached_text(None, 0, 5).startswith("⚠️ У вас уже 5 активных записей")


# ---------- деньги ----------


def test_format_money_default_and_custom():
    assert format_money(40) == "40 BYN" and format_money(40.5) == "40.50 BYN"
    assert format_money(40, "₽") == "40 ₽" and format_money("12.50", "USD") == "12.50 USD"
    assert format_money(40, "EUR") == f"{format_price(40)} EUR"


def test_reminder_texts_use_currency_and_keep_default():
    from app.utils.reminder_texts import client_reminder_24h_text, master_reminder_2h_text

    assert "💰 40 BYN" in client_reminder_24h_text("Маникюр", "15:00 - 16:00", 40)
    assert "💰 40 RUB" in client_reminder_24h_text("Маникюр", "15:00 - 16:00", 40, currency="RUB")
    assert "💰 40 RUB" in master_reminder_2h_text("Анна", "anna", "Маникюр", "15:00", 60, 40, currency="RUB")


def test_keyboard_labels_use_currency():
    from decimal import Decimal

    from app.bot.keyboards.client_keyboards import services_keyboard

    class S:
        id, name, price, duration_minutes = 1, "Маникюр", Decimal("40"), 60

    assert "40 BYN" in services_keyboard([S()]).inline_keyboard[0][0].text
    assert "40 ₴" in services_keyboard([S()], "₴").inline_keyboard[0][0].text


def test_reschedule_buttons_hidden_when_disabled():
    from app.bot.keyboards.client_keyboards import my_booking_keyboard
    from app.bot.keyboards.reminder_keyboards import reminder_keyboard

    def labels(markup):
        return [b.text for row in markup.inline_keyboard for b in row]

    assert "🔄 Перенести" in labels(my_booking_keyboard(1)) and "🔄 Перенести" in labels(reminder_keyboard(1))
    assert "🔄 Перенести" not in labels(my_booking_keyboard(1, None, False))
    assert "🔄 Перенести" not in labels(reminder_keyboard(1, False))
    assert "❌ Отменить" in labels(my_booking_keyboard(1, None, False))  # отмена остаётся


# ---------- шаблоны текстов ----------


def test_valid_template_roundtrip_and_all_fields():
    text = " ".join("{" + name + "}" for name in ALLOWED_FIELDS)
    assert validate_template(f"  {text}  ") == text
    rendered = render_template(text, EXAMPLE_VALUES, lambda: "DEFAULT")
    assert "DEFAULT" not in rendered and "{" not in rendered
    assert "Маникюр классический" in rendered and "18.09.2026" in rendered


@pytest.mark.parametrize(
    "bad",
    [
        "{unknown}", "{service.__class__}", "{0}", "{}", "{ service }", "{service!r}", "{service:>10}", "{SERVICE}",
        "{{service}}", "{", "}", "a } b", "{service", "text {client_name} {oops}", "{__import__}", "{service[0]}",
        "", "   ", "\x00{service}", "x" * 1001,
    ],
)
def test_bad_templates_are_rejected(bad):
    with pytest.raises(TemplateError):
        validate_template(bad)


def test_limit_exactly_1000_is_ok():
    assert len(validate_template("x" * 1000)) == 1000


def test_rejection_message_lists_allowed_fields():
    with pytest.raises(TemplateError) as err:
        validate_template("{nope}")
    assert "{service}" in str(err.value) and "{nope}" in str(err.value)


def test_values_and_literal_text_are_html_escaped():
    out = render_template("<b>Привет</b>, {client_name}!", {"client_name": "<i>x</i> & \"y\""}, lambda: "D")
    assert "<" not in out and ">" not in out
    assert out.startswith("&lt;b&gt;Привет&lt;/b&gt;, &lt;i&gt;x&lt;/i&gt; &amp;")


def test_placeholder_values_are_not_reinterpreted():
    # значение, похожее на подстановку, не раскрывается повторно
    out = render_template("{client_name}", {"client_name": "{service}", "service": "SECRET"}, lambda: "D")
    assert out == "{service}"


def test_none_template_uses_default_and_invalid_template_falls_back():
    assert render_template(None, {}, lambda: "DEFAULT") == "DEFAULT"
    assert render_template("{broken}", {}, lambda: "DEFAULT") == "DEFAULT"
    assert render_template("x" * 5000, {}, lambda: "DEFAULT") == "DEFAULT"


def test_default_factory_not_called_when_template_is_fine():
    def boom():
        raise AssertionError("default не должен вычисляться")

    assert render_template("ok {service}", {"service": "A"}, boom) == "ok A"


def test_too_long_after_substitution_falls_back():
    out = render_template("{service}" * 200, {"service": "я" * 30}, lambda: "DEFAULT")
    assert len("я" * 30 * 200) > MAX_RENDERED_LEN and out == "DEFAULT"


def test_missing_value_renders_empty():
    assert render_template("a{address}b", {}, lambda: "D") == "ab"


def test_preview_uses_examples():
    assert render_preview("{client_name}, {time}") == "Анна, 15:00 - 16:00"
    with pytest.raises(TemplateError):
        render_preview("{x}")


# ---------- в коде не осталось «зашитых» значений ----------

# Файлы, где слово «BYN» осталось осознанно: значение по умолчанию, пример для подсказки, комментарии/докстринги.
BYN_ALLOWED = {
    "app/utils/master_settings.py": "DEFAULT_CURRENCY = \"BYN\"",
    "app/database/models/master_settings.py": "server_default и default колонки currency",
    "app/utils/text_templates.py": "пример для предпросмотра",
    "app/utils/money.py": "докстринг format_money",
    "app/bot/handlers/admin/bookings.py": "пример формата в докстринге _format_booking_block",
}


def _app_files():
    return [p for p in sorted(APP_DIR.rglob("*.py")) if "__pycache__" not in p.parts]


def test_no_hardcoded_currency_left_in_app():
    offenders = []
    for path in _app_files():
        rel = path.relative_to(APP_DIR.parent).as_posix()
        if rel in BYN_ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "BYN" in line:
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert not offenders, "«BYN» должен приходить из настроек мастера:\n" + "\n".join(offenders)


def test_slot_interval_is_not_read_from_env_settings_in_handlers():
    offenders = [
        p.relative_to(APP_DIR.parent).as_posix()
        for p in _app_files()
        if "config" not in p.parts and re.search(r"\bsettings\.slot_interval_minutes\b", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"интервал слотов — настройка мастера, а не .env: {offenders}"


def test_handlers_do_not_use_global_limits_directly():
    """Лимит записей и порог отмен берутся из настроек мастера; константы остались только значениями по умолчанию."""
    offenders = []
    for path in _app_files():
        rel = path.relative_to(APP_DIR.parent).as_posix()
        if rel == "app/services/booking_service.py":
            continue
        text = path.read_text(encoding="utf-8")
        for name in ("CANCEL_STREAK_ALERT", "MAX_ACTIVE_BOOKINGS"):
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(rf"\b{name}\b", line) and "import" not in line and "limit: int = " not in line:
                    offenders.append(f"{rel}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_booking_service_is_created_from_settings_in_handlers():
    offenders = []
    for path in (APP_DIR / "bot" / "handlers").rglob("*.py"):
        if re.search(r"\bBookingService\(", path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert not offenders, f"в хендлерах сервис создаётся через booking_service_for(): {offenders}"
