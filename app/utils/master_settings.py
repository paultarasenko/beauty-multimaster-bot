"""Индивидуальные настройки мастера (этап 8): значения по умолчанию, допустимые границы и проверка ввода.

Чистый модуль без БД и aiogram: им пользуются и модель (CHECK-ограничения), и сервисы, и админский интерфейс.
Значения по умолчанию равны поведению бота до этапа 8 — мастер, который ничего не менял, разницы не замечает.
"""

import re
from dataclasses import dataclass, fields

# --- допустимые значения и значения по умолчанию ---
ALLOWED_SLOT_INTERVALS = (15, 30, 60)  # шаг сетки времени записи, минут
DEFAULT_SLOT_INTERVAL = 30

MIN_ACTIVE_BOOKINGS, MAX_ACTIVE_BOOKINGS_LIMIT = 1, 10  # лимит одновременно действующих записей клиента
DEFAULT_MAX_ACTIVE_BOOKINGS = 3

MIN_CANCEL_STREAK, MAX_CANCEL_STREAK = 2, 10  # сколько отмен подряд — повод предупредить мастера
DEFAULT_CANCEL_STREAK_LIMIT = 3

DEFAULT_CURRENCY = "BYN"
MAX_CURRENCY_LEN = 8
# Валюта попадает в HTML-сообщения, кнопки и Excel без экранирования: допускаем только «безопасные» символы
# (буквы, цифры и знаки валют), без пробелов, угловых скобок, амперсанда и фигурных скобок.
_CURRENCY_RE = re.compile(r"^[^\s<>&{}\x00-\x1f\x7f]+$")

# Часы напоминаний не настраиваются (остаются 24 и 2, этап 8).
REMINDER_24H_HOURS = 24
REMINDER_2H_HOURS = 2

MAX_TEMPLATE_LEN = 1000  # длина индивидуального текста (шаблона)

# Тексты-шаблоны: поле настройки -> человекочитаемое название (для интерфейса).
TEXT_FIELDS = {
    "booking_confirmation_text": "Подтверждение записи",
    "cancellation_text": "Отмена записи",
    "reminder_24h_text": "Напоминание за 24 часа",
    "reminder_2h_text": "Напоминание за 2 часа",
}

BOOL_FIELDS = {
    "reminder_24h_enabled": "Напоминание клиенту за 24 часа",
    "reminder_2h_enabled": "Напоминание клиенту за 2 часа",
    "master_reminder_2h_enabled": "Напоминание мастеру за 2 часа",
    "allow_reschedule": "Перенос записи клиентом",
    "allow_reviews": "Отзывы клиентов",
}


class SettingError(ValueError):
    """Недопустимое значение настройки; текст безопасен для показа мастеру."""


@dataclass(frozen=True)
class MasterSettingsData:
    """Снимок настроек мастера (не привязан к сессии БД). Значения по умолчанию = поведение до этапа 8."""

    slot_interval_minutes: int = DEFAULT_SLOT_INTERVAL
    reminder_24h_enabled: bool = True
    reminder_2h_enabled: bool = True
    master_reminder_2h_enabled: bool = True
    max_active_bookings: int = DEFAULT_MAX_ACTIVE_BOOKINGS
    cancel_streak_limit: int = DEFAULT_CANCEL_STREAK_LIMIT
    allow_reschedule: bool = True
    allow_reviews: bool = True
    currency: str = DEFAULT_CURRENCY
    booking_confirmation_text: str | None = None
    cancellation_text: str | None = None
    reminder_24h_text: str | None = None
    reminder_2h_text: str | None = None

    def disabled_notification_types(self) -> frozenset[str]:
        """Типы уведомлений (значения NotificationType), которые у этого мастера выключены."""
        disabled = set()
        if not self.reminder_24h_enabled:
            disabled.add("reminder_24h")
        if not self.reminder_2h_enabled:
            disabled.add("reminder_2h_client")
        if not self.master_reminder_2h_enabled:
            disabled.add("reminder_2h_master")
        return frozenset(disabled)


SETTING_FIELDS = tuple(f.name for f in fields(MasterSettingsData))


def default_value(field: str):
    """Значение настройки по умолчанию (для текстов — None: «текст по умолчанию из кода»)."""
    if field not in SETTING_FIELDS:
        raise KeyError(field)
    return getattr(MasterSettingsData(), field)


def settings_from_row(row) -> MasterSettingsData:
    """Снимок из строки master_settings (или любого объекта с теми же атрибутами)."""
    return MasterSettingsData(**{name: getattr(row, name) for name in SETTING_FIELDS})


# --- проверка ввода ---


def validate_slot_interval(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in ALLOWED_SLOT_INTERVALS:
        allowed = ", ".join(str(v) for v in ALLOWED_SLOT_INTERVALS)
        raise SettingError(f"Допустимый шаг записи — {allowed} минут.")
    return value


def validate_max_active_bookings(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_ACTIVE_BOOKINGS <= value <= MAX_ACTIVE_BOOKINGS_LIMIT:
        raise SettingError(f"Лимит записей — число от {MIN_ACTIVE_BOOKINGS} до {MAX_ACTIVE_BOOKINGS_LIMIT}.")
    return value


def validate_cancel_streak_limit(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_CANCEL_STREAK <= value <= MAX_CANCEL_STREAK:
        raise SettingError(f"Порог отмен подряд — число от {MIN_CANCEL_STREAK} до {MAX_CANCEL_STREAK}.")
    return value


def validate_currency(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > MAX_CURRENCY_LEN or not _CURRENCY_RE.match(text):
        raise SettingError(
            f"Валюта — от 1 до {MAX_CURRENCY_LEN} символов без пробелов, например BYN, RUB, USD, €, ₽."
        )
    return text


def parse_int_input(text: str | None) -> int:
    """Целое число из ввода мастера (только цифры, без знака)."""
    cleaned = (text or "").strip()
    if not cleaned.isascii() or not cleaned.isdigit() or len(cleaned) > 4:
        raise SettingError("Введите целое число.")
    return int(cleaned)


def validate_bool(value) -> bool:
    if not isinstance(value, bool):
        raise SettingError("Значение должно быть «включено» или «выключено».")
    return value


def validate_setting(field: str, value):
    """Проверяет значение настройки (кроме текстов — они проверяются в text_templates) и возвращает его очищенным."""
    if field == "slot_interval_minutes":
        return validate_slot_interval(value)
    if field == "max_active_bookings":
        return validate_max_active_bookings(value)
    if field == "cancel_streak_limit":
        return validate_cancel_streak_limit(value)
    if field == "currency":
        return validate_currency(value)
    if field in BOOL_FIELDS:
        return validate_bool(value)
    raise SettingError("Неизвестная настройка.")


# --- склонение числа записей для текстов с лимитом ---
_GENITIVE_COUNT = {
    1: "одной",
    2: "двух",
    3: "трёх",
    4: "четырёх",
    5: "пяти",
    6: "шести",
    7: "семи",
    8: "восьми",
    9: "девяти",
    10: "десяти",
}


def count_genitive(n: int) -> str:
    """«трёх» для 3 (для фразы «больше трёх записей»); числа вне словаря — цифрами."""
    return _GENITIVE_COUNT.get(n, str(n))


def client_reminder_phrase(s: MasterSettingsData) -> str | None:
    """Когда клиенту придут напоминания: «за 24 и за 2 часа» / «за 24 часа» / «за 2 часа»; None — оба выключены.

    Для текстов «Напомним о ней …»: при настройках по умолчанию совпадает с прежним «за 24 и за 2 часа».
    """
    if s.reminder_24h_enabled and s.reminder_2h_enabled:
        return f"за {REMINDER_24H_HOURS} и за {REMINDER_2H_HOURS} часа"
    if s.reminder_24h_enabled:
        return f"за {REMINDER_24H_HOURS} часа"
    if s.reminder_2h_enabled:
        return f"за {REMINDER_2H_HOURS} часа"
    return None
