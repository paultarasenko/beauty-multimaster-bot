import datetime as dt

import pytest

from app.database.models import WorkingHours
from app.services.booking_service import BookingService
from tests.conftest import MASTER_ID, next_weekday

RIGA_TZ = dt.timezone(dt.timedelta(hours=2))  # летнее время Риги, для простоты фиксированный offset
UTC = dt.timezone.utc
NEXT_FRIDAY = next_weekday(4)  # пятница через 2+ недели: дата всегда в будущем


@pytest.mark.asyncio
async def test_booking_stored_as_utc_but_matches_local_wall_clock(session, test_user, manicure_service):
    """Клиент выбирает 15:00 по местному времени мастера (Europe/Riga, UTC+2).
    В БД должно храниться корректное абсолютное время (13:00 UTC), но при чтении
    обратно с tz-aware datetime это должно давать те же 15:00 по Риге."""
    session.add(
        WorkingHours(master_id=MASTER_ID,
            weekday=NEXT_FRIDAY.weekday(), is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0)
        )
    )
    await session.commit()

    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    local_start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)
    booking = await svc.create_booking(test_user.id, manicure_service.id, local_start)
    await session.commit()

    refreshed = await svc.bookings.get_by_id(booking.id)
    assert refreshed.start_datetime.astimezone(UTC) == local_start.astimezone(UTC)
    assert refreshed.start_datetime.astimezone(RIGA_TZ).hour == 15


@pytest.mark.asyncio
async def test_naive_datetime_is_rejected_or_normalized(session, test_user, manicure_service):
    """Если бы кто-то передал naive datetime без tzinfo, это должно быть явно предотвращено —
    working_hours сравнение опирается на tz-aware даты, naive datetime здесь вызовет ошибку
    сравнения tz-aware/naive, а не тихо создаст неправильную запись."""
    session.add(
        WorkingHours(master_id=MASTER_ID,
            weekday=NEXT_FRIDAY.weekday(), is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0)
        )
    )
    await session.commit()

    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    naive_start = dt.datetime(2026, 10, 16, 15, 0)  # без tzinfo

    with pytest.raises(TypeError):
        await svc.create_booking(test_user.id, manicure_service.id, naive_start)


def test_format_time_range_local():
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app.utils.timezone import format_time_range

    tz = ZoneInfo("Europe/Minsk")
    start = dt.datetime(2026, 10, 2, 15, 0, tzinfo=tz)
    assert format_time_range(start, start + dt.timedelta(minutes=90)) == "15:00 - 16:30"


def test_format_time_range_converts_utc_to_master_tz():
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app.utils.timezone import format_time_range

    tz = ZoneInfo("Europe/Minsk")  # UTC+3
    start = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
    assert format_time_range(start, start + dt.timedelta(minutes=90), tz) == "15:00 - 16:30"
