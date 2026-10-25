import datetime as dt
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.services.availability_service import Interval
from app.utils.timezone import MONTH_NAMES_RU

# Шрифт Jost (лицензия OFL) лежит в проекте, поэтому картинка выглядит одинаково на любом сервере.
# Это вариативный шрифт: один файл, нужная толщина задаётся при загрузке.
_FONT_FILE = str(Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Jost.ttf")
_FONT_BOLD = _FONT_SEMIBOLD = _FONT_REGULAR = _FONT_FILE
_WEIGHT_TITLE = 600
_WEIGHT_ROW = 500
_WEIGHT_LEGEND = 400


def _load_font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(_FONT_FILE, size)
    font.set_variation_by_axes([weight])
    return font


_WEEKDAY_NAMES_RU_FULL = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

_SLOT_STEP_MINUTES = 30

_WIDTH = 750
_PADDING = 20
_TITLE_HEIGHT = 50
_ROW_HEIGHT = 56
_ROW_GAP = 8
_LEGEND_HEIGHT = 50

_COLOR_BG = (250, 249, 245)  # #faf9f5
_COLOR_FREE = (252, 252, 251)  # #fcfcfb — свободно
_COLOR_FREE_BORDER = (222, 221, 218)  # #deddda — рамка свободного слота
_COLOR_FREE_TEXT = (26, 26, 26)  # #1a1a1a
_COLOR_BUSY = (219, 119, 87)  # #db7757 — занято
_COLOR_BUSY_TEXT = (26, 26, 26)
_COLOR_TITLE = (26, 26, 26)
_COLOR_ACCENT = (219, 119, 87)  # линия под заголовком
_ROW_RADIUS = 18


def _slot_rows(
    date: dt.date,
    tz: dt.tzinfo,
    start_time: dt.time,
    end_time: dt.time,
    free_intervals: list[Interval],
) -> list[tuple[dt.datetime, dt.datetime, bool]]:
    """Разбивает рабочий день на строки по получасовой сетке и помечает каждую свободна/занята.

    Границы свободных интервалов, не попадающие в сетку (например, 14:20 — конец записи),
    тоже становятся границами строк: иначе строка 14:00–14:30 целиком помечалась бы «Занято»,
    хотя с 14:20 время свободно и доступно для записи.
    """
    day_start = dt.datetime.combine(date, start_time, tzinfo=tz)
    day_end = dt.datetime.combine(date, end_time, tzinfo=tz)
    step = dt.timedelta(minutes=_SLOT_STEP_MINUTES)

    boundaries: set[dt.datetime] = {day_end}
    cursor = day_start
    while cursor < day_end:
        boundaries.add(cursor)
        cursor += step
    for iv in free_intervals:
        # Границы интервалов из БД приходят в UTC — приводим к таймзоне мастера для подписей строк.
        for point in (iv.start.astimezone(tz), iv.end.astimezone(tz)):
            if day_start < point < day_end:
                boundaries.add(point)
    points = sorted(boundaries)

    rows: list[tuple[dt.datetime, dt.datetime, bool]] = []
    for slot_start, slot_end in zip(points, points[1:]):
        is_free = any(iv.start <= slot_start and slot_end <= iv.end for iv in free_intervals)
        rows.append((slot_start, slot_end, is_free))
    return rows


def _merge_rows(
    rows: list[tuple[dt.datetime, dt.datetime, bool]],
) -> list[tuple[dt.datetime, dt.datetime, bool]]:
    """Склеивает подряд идущие строки с одинаковым статусом в одну.

    Занято: 10:30–12:30 вместо трёх строк по получаса.
    Свободно: 14:50–15:30 вместо двух (14:50–15:00 и 15:00–15:30) — окно между записями одним слотом.
    """
    merged: list[tuple[dt.datetime, dt.datetime, bool]] = []
    for slot_start, slot_end, is_free in rows:
        if merged and merged[-1][2] == is_free and merged[-1][1] == slot_start:
            merged[-1] = (merged[-1][0], slot_end, is_free)
        else:
            merged.append((slot_start, slot_end, is_free))
    return merged


def render_day_schedule_image(
    date: dt.date,
    tz: dt.tzinfo,
    start_time: dt.time,
    end_time: dt.time,
    free_intervals: list[Interval],
) -> bytes:
    """Рисует PNG-картинку расписания дня: зелёный — свободно, тёмный — занято.
    Подряд идущие строки с одинаковым статусом объединены в одну."""
    rows = _slot_rows(date, tz, start_time, end_time, free_intervals)

    # Убираем строки, которые целиком уже в прошлом (и свободные, и занятые) —
    # для будущих дат now всегда меньше начала дня, так что фильтр их не затрагивает.
    # Фильтруем до склейки, чтобы объединённое свободное окно не начиналось в прошлом.
    now = dt.datetime.now(tz)
    rows = _merge_rows([row for row in rows if row[1] > now])

    height = _PADDING * 2 + _TITLE_HEIGHT + len(rows) * (_ROW_HEIGHT + _ROW_GAP) + _LEGEND_HEIGHT
    image = Image.new("RGB", (_WIDTH, height), _COLOR_BG)
    draw = ImageDraw.Draw(image)

    title_font = _load_font(28, _WEIGHT_TITLE)
    row_font = _load_font(22, _WEIGHT_ROW)
    legend_font = _load_font(19, _WEIGHT_LEGEND)

    title = f"{_WEEKDAY_NAMES_RU_FULL[date.weekday()]}, {date.day} {MONTH_NAMES_RU[date.month - 1]}"
    draw.text((_PADDING, _PADDING), title, font=title_font, fill=_COLOR_TITLE)
    line_y = _PADDING + _TITLE_HEIGHT - 12
    draw.rounded_rectangle([(_PADDING, line_y), (_PADDING + 56, line_y + 3)], radius=2, fill=_COLOR_ACCENT)

    y = _PADDING + _TITLE_HEIGHT
    for slot_start, slot_end, is_free in rows:
        color = _COLOR_FREE if is_free else _COLOR_BUSY
        text_color = _COLOR_FREE_TEXT if is_free else _COLOR_BUSY_TEXT
        label = f"{slot_start.strftime('%H:%M')}–{slot_end.strftime('%H:%M')}"
        status = "Свободно" if is_free else "Занято"

        draw.rounded_rectangle(
            [(_PADDING, y), (_WIDTH - _PADDING, y + _ROW_HEIGHT)],
            radius=_ROW_RADIUS,
            fill=color,
            outline=_COLOR_FREE_BORDER if is_free else None,
            width=2,
        )
        draw.text((_PADDING + 20, y + _ROW_HEIGHT / 2), label, font=row_font, fill=text_color, anchor="lm")

        bbox = draw.textbbox((0, 0), status, font=row_font)
        status_width = bbox[2] - bbox[0]
        draw.text(
            (_WIDTH - _PADDING - 20 - status_width, y + _ROW_HEIGHT / 2),
            status,
            font=row_font,
            fill=text_color,
            anchor="lm",
        )
        y += _ROW_HEIGHT + _ROW_GAP

    legend_y = y + 12
    swatch = 20
    draw.ellipse(
        [(_PADDING, legend_y), (_PADDING + swatch, legend_y + swatch)], fill=_COLOR_FREE, outline=_COLOR_FREE_BORDER, width=2
    )
    draw.text(
        (_PADDING + swatch + 10, legend_y + swatch / 2), "Свободно", font=legend_font, fill=_COLOR_TITLE, anchor="lm"
    )
    legend_x2 = _WIDTH / 2
    draw.ellipse([(legend_x2, legend_y), (legend_x2 + swatch, legend_y + swatch)], fill=_COLOR_BUSY)
    draw.text(
        (legend_x2 + swatch + 10, legend_y + swatch / 2), "Занято", font=legend_font, fill=_COLOR_TITLE, anchor="lm"
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
