"""Номер телефона клиента, который Telegram отдаёт по кнопке «Поделиться номером».

Telegram присылает номер то с «+», то без («375291234567»), иногда с пробелами. Приводим к виду «+375291234567».
"""
import re

_PHONE_MIN_DIGITS = 7
_PHONE_MAX_DIGITS = 15  # максимум по E.164


def normalize_phone(raw: str | None) -> str | None:
    """«+375291234567» или None, если в строке нет похожего на номер набора цифр."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not (_PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS):
        return None
    return f"+{digits}"
