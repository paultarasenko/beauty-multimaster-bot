"""Общее для нагрузочного стенда: защита базы, метки тестовых данных, перцентили."""

import math
import os
from collections.abc import Sequence

from sqlalchemy.engine import make_url

DB_ENV = "LOADTEST_DATABASE_URL"
REQUIRED_DB_PREFIX = "loadtest"

# Всё тестовое помечено числами ≥ TEST_OWNER_MIN: по ним seed --cleanup находит и удаляет только свои данные.
TEST_OWNER_MIN = 900_000_000_000  # masters.owner_telegram_id тестовых мастеров
OWNER_BASE = 900_000_000_000  # владелец мастера №i = OWNER_BASE + i
CLIENT_BASE = 910_000_000_000  # клиент k мастера №i = CLIENT_BASE + i * 10_000 + k (users.telegram_id)
BOT_ID_BASE = 7_900_000_000  # id бота мастера №i в «Telegram» = BOT_ID_BASE + i


class GuardError(SystemExit):
    """Отказ работать: код выхода 2, текст — для оператора."""

    def __init__(self, message: str):
        super().__init__(f"ОТКАЗ: {message}")
        self.code = 2


def require_loadtest_db_url(url: str | None = None) -> str:
    """Адрес ТОЛЬКО тестовой базы из LOADTEST_DATABASE_URL (DATABASE_URL не читается никогда).

    Имя базы обязано начинаться с «loadtest»: так нагрузочный сценарий не может случайно записать данные в рабочую базу.
    Возвращает адрес в формате SQLAlchemy (postgresql+asyncpg://...).
    """
    url = url if url is not None else os.environ.get(DB_ENV, "")
    if not url.strip():
        raise GuardError(f"не задана переменная {DB_ENV} (адрес тестовой базы; DATABASE_URL стенд не использует)")
    try:
        parsed = make_url(url.strip())
    except Exception:
        raise GuardError(f"{DB_ENV} не похож на адрес базы данных") from None
    name = parsed.database or ""
    if not name.startswith(REQUIRED_DB_PREFIX):
        raise GuardError(
            f"имя базы в {DB_ENV} должно начинаться с «{REQUIRED_DB_PREFIX}» (сейчас «{name}»). "
            "Стенд пишет и удаляет данные, поэтому работает только с отдельной тестовой базой."
        )
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.render_as_string(hide_password=False)


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    """Тот же адрес для asyncpg.connect (без «+asyncpg»)."""
    return make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Перцентиль p (0–100) методом «ближайшего ранга»: наименьшее значение, не меньше которого p % выборки. Пусто — None."""
    if not values:
        return None
    if not 0 <= p <= 100:
        raise ValueError("p должно быть от 0 до 100")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return float(ordered[rank - 1])
