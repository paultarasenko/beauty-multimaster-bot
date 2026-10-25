from decimal import Decimal

from app.utils.money import format_price


def test_format_price_drops_zero_cents():
    assert format_price(Decimal("120.00")) == "120"
    assert format_price("120.00") == "120"
    assert format_price(40) == "40"
    assert format_price(40.0) == "40"
    assert format_price(Decimal("0.00")) == "0"


def test_format_price_keeps_real_cents():
    assert format_price(Decimal("40.50")) == "40.50"
    assert format_price(40.5) == "40.50"
    assert format_price(Decimal("12.05")) == "12.05"


def test_format_price_non_numeric_returned_as_is():
    assert format_price(None) == "None"
    assert format_price("abc") == "abc"
