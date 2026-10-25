import asyncio
import datetime as dt

import pytest

from app.database.models import WorkingHours
from app.database.session import make_session_factory
from app.services.booking_service import BookingService
from app.services.exceptions import SlotNotAvailableError
from tests.conftest import MASTER_ID, TEST_DATABASE_URL
from tests.conftest import next_weekday

RIGA_TZ = dt.timezone(dt.timedelta(hours=2))
NEXT_FRIDAY = next_weekday(4)  # пятница через 2+ недели: дата всегда в будущем


@pytest.mark.asyncio
async def test_concurrent_booking_only_one_wins(session, test_user, test_user_2, manicure_service):
    """Два клиента одновременно жмут на одно и то же время 15:00.
    Только один должен получить подтверждённую запись, второй — SlotNotAvailableError.
    Каждый запрос идёт в СВОЕЙ транзакции/сессии (как два разных Telegram-апдейта).
    """
    session.add(
        WorkingHours(master_id=MASTER_ID,
            weekday=NEXT_FRIDAY.weekday(), is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0)
        )
    )
    await session.commit()

    start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)
    session_factory = make_session_factory(TEST_DATABASE_URL)

    async def attempt(user_id: int):
        async with session_factory() as s:
            svc = BookingService(s, RIGA_TZ, master_id=MASTER_ID)
            try:
                booking = await svc.create_booking(user_id, manicure_service.id, start)
                await s.commit()
                return ("ok", booking.id)
            except SlotNotAvailableError:
                return ("rejected", None)

    results = await asyncio.gather(
        attempt(test_user.id), attempt(test_user_2.id), return_exceptions=False
    )

    outcomes = [r[0] for r in results]
    assert outcomes.count("ok") == 1, f"expected exactly one success, got {results}"
    assert outcomes.count("rejected") == 1, f"expected exactly one rejection, got {results}"
