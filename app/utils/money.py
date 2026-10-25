"""Форматирование денежных сумм для текстов бота и отчётов."""

from decimal import Decimal, InvalidOperation

from app.utils.master_settings import DEFAULT_CURRENCY


def format_price(price) -> str:
    """120.00 -> «120», 120.50 -> «120.50». Нечисловое значение возвращается как есть."""
    try:
        value = Decimal(str(price))
    except (InvalidOperation, ValueError):
        return str(price)
    if not value.is_finite():
        return str(price)
    if value == value.to_integral_value():
        return str(int(value))
    return f"{value:.2f}"


def format_money(price, currency: str = DEFAULT_CURRENCY) -> str:
    """Сумма с валютой мастера: 40 -> «40 BYN», 40.5 -> «40.50 BYN» (валюта — настройка мастера, этап 8)."""
    return f"{format_price(price)} {currency}"
