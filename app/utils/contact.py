"""Проверка контакта клиента, который мастер вводит при ручной записи: телефон или @username Telegram.

Бот не угадывает, что ввод без «@» — это username: так запись можно привязать к постороннему человеку со случайно
совпавшим ником. Всё, что не похоже ни на телефон, ни на @username, мастер вводит заново.
"""
import re

_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")  # правила Telegram: 5-32 символа, первая — буква
_PHONE_RE = re.compile(r"^\+?[\d\s\-()]+$")
_PHONE_MIN_DIGITS = 7
_PHONE_MAX_DIGITS = 15  # максимум по E.164

CONTACT_PROMPT = "Введите контакт клиента (телефон или @username):"
CONTACT_INVALID_TEXT = (
    "⚠️ Не похоже на контакт.\n\n"
    "Введите телефон (например, +375291234567) или Telegram-ник с «@» (например, @anna_k):"
)


def normalize_contact(raw: str | None) -> str | None:
    """Контакт без лишних пробелов по краям или None, если это не телефон и не @username."""
    if raw is None:
        return None
    contact = raw.strip()
    if _USERNAME_RE.match(contact):
        return contact
    if _PHONE_RE.match(contact) and _PHONE_MIN_DIGITS <= sum(ch.isdigit() for ch in contact) <= _PHONE_MAX_DIGITS:
        return contact
    return None
