"""Пробный период мастера (этап 10): чистые функции расчёта срока и статуса. Без БД и Telegram.

Срок — `masters.trial_ends_on`: ПОСЛЕДНИЙ день, когда он действует (включительно); None — «без срока». Срок ничего не
отключает: он лишь показывает суперадмину, когда пора принять решение. «Сегодня» считается в часовом поясе суперадмина
(SUPERADMIN_TIMEZONE, по умолчанию как в проекте).
"""

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.config.settings import settings

KIND_NONE = "none"  # без срока
KIND_ACTIVE = "active"  # срок действует (включая последний день)
KIND_EXPIRED = "expired"  # срок закончился — нужно решение

MAX_TRIAL_DAYS = 3650
QUICK_DAYS = (7, 14, 30)


def superadmin_tz() -> ZoneInfo:
    return ZoneInfo(settings.superadmin_timezone or settings.timezone)


def superadmin_today(now: dt.datetime | None = None) -> dt.date:
    """Сегодняшняя дата по часам суперадмина. `now` — для тестов (aware-время)."""
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(superadmin_tz()).date()


@dataclass(frozen=True)
class TrialStatus:
    kind: str
    ends_on: dt.date | None
    days: int = 0  # active: сколько дней осталось (0 — сегодня последний день); expired: сколько дней назад закончился

    @property
    def needs_decision(self) -> bool:
        """Срок истёк или истекает завтра (последний день — завтра)."""
        return self.kind == KIND_EXPIRED or (self.kind == KIND_ACTIVE and self.days == 1)

    @property
    def expires_tomorrow(self) -> bool:
        return self.kind == KIND_ACTIVE and self.days == 1


def trial_status(ends_on: dt.date | None, today: dt.date) -> TrialStatus:
    if ends_on is None:
        return TrialStatus(KIND_NONE, None)
    if ends_on >= today:
        return TrialStatus(KIND_ACTIVE, ends_on, (ends_on - today).days)
    return TrialStatus(KIND_EXPIRED, ends_on, (today - ends_on).days)


def days_word(n: int) -> str:
    """«1 дн.» одинаково для любого числа — короткая форма без склонений."""
    return f"{n} дн."


def status_text(status: TrialStatus) -> str:
    """Текст статуса срока для списка и карточки суперадмина."""
    if status.kind == KIND_NONE:
        return "без срока"
    assert status.ends_on is not None
    date = status.ends_on.strftime("%d.%m")
    if status.kind == KIND_ACTIVE:
        left = "сегодня последний день" if status.days == 0 else f"осталось {days_word(status.days)}"
        return f"пробный до {date} ({left})"
    return f"пробный закончился {days_word(status.days)} назад — нужно решение"


def status_icon(status: TrialStatus) -> str:
    if status.kind == KIND_EXPIRED:
        return "⛔"
    if status.needs_decision:
        return "⏳"
    return ""


def extended_ends_on(current: dt.date | None, today: dt.date, days: int) -> dt.date:
    """Новый срок при продлении на `days` дней: от сегодняшней даты, если срока нет или он уже истёк, иначе от текущего окончания."""
    base = current if current is not None and current >= today else today
    return base + dt.timedelta(days=days)


def new_master_ends_on(days: int | None, today: dt.date) -> dt.date | None:
    """Срок нового мастера: `days` дней от сегодня; None — без срока."""
    return None if days is None else today + dt.timedelta(days=days)


def parse_days(raw: str) -> int | None:
    """Число дней из текста суперадмина: целое от 1 до MAX_TRIAL_DAYS, иначе None."""
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if 1 <= value <= MAX_TRIAL_DAYS else None
