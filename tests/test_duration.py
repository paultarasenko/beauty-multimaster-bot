import pytest

from app.utils.duration import format_duration


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (1, "1 мин"),
        (45, "45 мин"),
        (59, "59 мин"),
        (60, "1 ч"),
        (85, "1 ч 25 мин"),
        (90, "1 ч 30 мин"),
        (120, "2 ч"),
        (150, "2 ч 30 мин"),
        (180, "3 ч"),
        (0, "0 мин"),
    ],
)
def test_format_duration(minutes, expected):
    assert format_duration(minutes) == expected


def test_format_duration_non_numeric_returned_as_is():
    assert format_duration("abc") == "abc"
