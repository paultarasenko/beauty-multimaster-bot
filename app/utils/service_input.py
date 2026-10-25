"""Разбор и проверка того, что мастер вводит текстом при добавлении и редактировании услуги.

Одни и те же правила нужны и в сценарии «добавить», и в «изменить»: раньше они жили в хендлерах по отдельности,
и «добавить» пропускал то, что «изменить» отбрасывал (`nan` как цену, пустое название, длительность в миллиарды минут).
"""

import re
from decimal import ROUND_HALF_UP, Decimal

MAX_NAME_LEN = 255  # длина столбца name в БД
MAX_PRICE = Decimal(99_999_999)  # столбец price — Numeric(10, 2)
MAX_DESCRIPTION_LEN = 1000  # описание попадает в сообщение с прайсом, лимит Telegram — 4096
MAX_DURATION_MINUTES = 1440  # не больше суток
CLEAR_DESCRIPTION = "-"  # этим символом (или словом «нет») описание очищается
CLEAR_DESCRIPTION_WORDS = {CLEAR_DESCRIPTION, "нет"}

# Только обычные десятичные записи: Decimal/float принимают ещё «nan», «inf», «1e5» и «1_000» — это не цена.
_PRICE_RE = re.compile(r"[0-9]+(?:[.,][0-9]+)?")
_DURATION_RE = re.compile(r"[0-9]{1,4}")  # только минуты: «45», «90»
_DURATION_CLOCK_RE = re.compile(r"([0-9]{1,2}):([0-5][0-9])")  # «1:20»
# «1 20» (часы и минуты через пробел — на телефоне двоеточие искать не надо), «1ч20», «1 ч 20»
_DURATION_SPACED_RE = re.compile(r"([0-9]{1,2})(?:\s+|\s*ч(?:ас(?:а|ов)?)?\.?\s*)([0-9]{1,2})")
# «1 ч 20 мин», «1ч20м», «2 часа», «45 минут» — часы и/или минуты (только ASCII-цифры, не «²» и не «٦٠»).
_DURATION_HM_RE = re.compile(
    r"(?:([0-9]{1,2})\s*ч(?:ас(?:а|ов)?)?\.?)?\s*(?:([0-9]{1,4})\s*м(?:ин(?:ут[аы]?)?)?\.?)?"
)

# Быстрый выбор длительности кнопками: от 15 минут до 3 часов с шагом 15 минут.
DURATION_PRESETS = tuple(range(15, 181, 15))

# Свободное время после услуги (подготовка к следующему клиенту): кнопки в минутах, 0 — без свободного времени.
BUFFER_PRESETS = (0, 5, 10, 15, 30, 60)
BUFFER_PROMPT = "⏳ Выберите сколько необходимо свободного времени между клиентами на данную услугу"

# Подсказка при вводе длительности (и при добавлении, и при изменении услуги).
DURATION_PROMPT = (
    "Выберите длительность кнопкой или введите другое время вручную.\n\n"
    "Вручную — в минутах, например 45\n"
    "или в часах и минутах через пробел, например 1 20 (это 1 ч 20 мин)"
)


class InvalidServiceInput(ValueError):
    """Текст ввода не подходит. str(exc) — готовое сообщение мастеру."""


def parse_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise InvalidServiceInput(f"Введите название текстом, не длиннее {MAX_NAME_LEN} символов.")
    return name


def parse_price(raw: str | None) -> Decimal:
    text = (raw or "").strip()
    if not _PRICE_RE.fullmatch(text):
        raise InvalidServiceInput("Введите цену числом, например 40 или 40.50")
    price = Decimal(text.replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if price > MAX_PRICE:
        raise InvalidServiceInput("Введите цену числом, например 40 или 40.50")
    return price


def parse_duration(raw: str | None) -> int:
    """Длительность в минутах из «45», «1 20», «1:20», «1 ч 20 мин», «2 ч», «90 мин». От 1 минуты до суток.

    Одно число без единиц — всегда минуты («120» = 2 ч). Часы и минуты вместе вводятся через пробел или «:».
    """
    error = InvalidServiceInput(
        "Не удалось разобрать длительность.\n"
        "Введите в минутах, например 45, или в часах и минутах через пробел, например 1 20 "
        f"(от 1 минуты до {MAX_DURATION_MINUTES // 60} ч)."
    )
    text = (raw or "").strip().lower()
    total: int | None = None

    if _DURATION_RE.fullmatch(text):
        total = int(text)
    elif match := _DURATION_CLOCK_RE.fullmatch(text):
        total = int(match.group(1)) * 60 + int(match.group(2))
    elif match := _DURATION_SPACED_RE.fullmatch(text):
        if int(match.group(2)) >= 60:
            raise error
        total = int(match.group(1)) * 60 + int(match.group(2))
    elif text and (match := _DURATION_HM_RE.fullmatch(text)):
        hours, minutes = match.group(1), match.group(2)
        if hours is None and minutes is None:
            raise error
        if hours is not None and minutes is not None and int(minutes) >= 60:
            raise error  # «1 ч 90 мин» — неоднозначно, просим ввести иначе
        total = int(hours or 0) * 60 + int(minutes or 0)

    if total is None or not 1 <= total <= MAX_DURATION_MINUTES:
        raise error
    return total


def parse_description(raw: str | None) -> str | None:
    """Текст описания; None — описание нужно убрать («-» или «нет»)."""
    text = (raw or "").strip()
    if not text:
        raise InvalidServiceInput("Отправьте описание текстом.")
    # Описание клиенту отправляется в режиме HTML — угловые скобки его сломают.
    if "<" in text or ">" in text:
        raise InvalidServiceInput("Символы < и > использовать нельзя. Отправьте текст без них.")
    if len(text) > MAX_DESCRIPTION_LEN:
        raise InvalidServiceInput(f"Слишком длинное описание: максимум {MAX_DESCRIPTION_LEN} символов.")
    return None if text.lower() in CLEAR_DESCRIPTION_WORDS else text
