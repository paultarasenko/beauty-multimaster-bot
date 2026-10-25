import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.database.repositories.content_repository import ContentRepository

MONTH_NAMES_RU = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

WEEKDAY_NAMES_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]


async def get_master_tz(session: AsyncSession, master_id: int) -> dt.tzinfo:
    """Timezone мастера — из его master_profile, если он уже создан, иначе fallback на .env."""
    content_repo = ContentRepository(session, master_id)
    profile = await content_repo.get_master_profile()
    tz_name = profile.timezone if profile else settings.timezone
    return ZoneInfo(tz_name)


def to_local(value: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """Приводит tz-aware datetime к таймзоне мастера перед показом пользователю.

    Bookings хранятся в PostgreSQL как timestamptz. Пока объект Booking живёт в той же
    сессии, где был создан (expire_on_commit=False), start_datetime остаётся тем же
    Python-объектом, что был передан при создании (уже в локальной tz) — выглядит корректно.
    Но как только запись перечитывается свежим SELECT-ом (другой хендлер, другой процесс —
    воркер напоминаний, список записей у админа/клиента), asyncpg возвращает datetime в UTC,
    а не в таймзоне мастера — без этой конвертации время "плывёт" на пару часов.
    astimezone() на уже-локальном datetime — no-op по настенному времени, поэтому вызывать
    эту функцию можно всегда перед форматированием, не разбираясь, откуда взялось значение.
    """
    return value.astimezone(tz)


def format_time_range(start: dt.datetime, end: dt.datetime, tz: dt.tzinfo | None = None) -> str:
    """Интервал процедуры в виде «15:00 - 16:30».

    Если передан tz — оба конца сначала приводятся к таймзоне мастера (значения из БД
    приходят в UTC). Без tz datetime считаются уже локальными.
    """
    if tz is not None:
        start = to_local(start, tz)
        end = to_local(end, tz)
    return f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"


def format_date_ru(value: dt.date | dt.datetime) -> str:
    """Дата в формате «18 сентября» — не завязана на системную локаль контейнера."""
    return f"{value.day} {MONTH_NAMES_RU[value.month - 1]}"


def weekday_name_ru(value: dt.date | dt.datetime) -> str:
    """Название дня недели на русском, например «Вторник»."""
    return WEEKDAY_NAMES_RU[value.weekday()]
