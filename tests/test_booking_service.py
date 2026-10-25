import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Booking, BookingStatus, Notification, WorkingHours
from app.services.booking_service import BookingService
from app.services.exceptions import BookingNotCancellableError, SlotNotAvailableError
from tests.conftest import MASTER_ID, next_weekday

RIGA_TZ = dt.timezone(dt.timedelta(hours=2))
NEXT_FRIDAY = next_weekday(4)  # пятница через 2+ недели: дата всегда в будущем


async def _setup_working_hours(session: AsyncSession, weekday: int):
    session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0)))
    await session.flush()
    await session.commit()


@pytest.mark.asyncio
async def test_create_booking_success(session, test_user, manicure_service):
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)

    booking = await svc.create_booking(test_user.id, manicure_service.id, start)
    await session.commit()

    assert booking.id is not None
    assert booking.status == BookingStatus.CONFIRMED.value
    assert booking.service_price_snapshot == manicure_service.price
    assert booking.service_name_snapshot == manicure_service.name


@pytest.mark.asyncio
async def test_create_booking_snapshot_survives_price_change(session, test_user, manicure_service):
    """Ключевое требование ТЗ: если мастер потом поменял цену, старая запись сохраняет старую."""
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)
    booking = await svc.create_booking(test_user.id, manicure_service.id, start)
    await session.commit()

    manicure_service.price = 999
    await session.commit()

    refreshed = await svc.bookings.get_by_id(booking.id)
    assert refreshed.service_price_snapshot == 40  # старое значение, а не 999


@pytest.mark.asyncio
async def test_create_booking_rejects_taken_slot(session, test_user, test_user_2, manicure_service):
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)

    await svc.create_booking(test_user.id, manicure_service.id, start)
    await session.commit()

    with pytest.raises(SlotNotAvailableError):
        await svc.create_booking(test_user_2.id, manicure_service.id, start)


@pytest.mark.asyncio
async def test_client_can_have_multiple_future_bookings(session, test_user, manicure_service):
    """Требование из ТЗ: повторная запись клиенту разрешена, не блокируем после одной активной."""
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    start1 = dt.datetime.combine(NEXT_FRIDAY, dt.time(11, 0), tzinfo=RIGA_TZ)
    start2 = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)

    b1 = await svc.create_booking(test_user.id, manicure_service.id, start1)
    await session.commit()
    b2 = await svc.create_booking(test_user.id, manicure_service.id, start2)
    await session.commit()

    assert b1.id != b2.id
    upcoming = await svc.bookings.get_user_upcoming(test_user.id, dt.datetime.now(RIGA_TZ))
    assert len(upcoming) == 2


@pytest.mark.asyncio
async def test_cancel_booking_frees_slot_and_cancels_reminder(session, test_user, test_user_2, manicure_service):
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)

    booking = await svc.create_booking(test_user.id, manicure_service.id, start)
    await session.commit()

    cancelled = await svc.cancel_booking(booking.id, now=dt.datetime(2026, 10, 1, tzinfo=RIGA_TZ))
    await session.commit()
    assert cancelled.status == BookingStatus.CANCELLED.value

    # слот освободился — второй клиент может занять то же время
    new_booking = await svc.create_booking(test_user_2.id, manicure_service.id, start)
    await session.commit()
    assert new_booking.id is not None

    notif = await session.get(Notification, 1)
    assert notif.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_cancelled_booking_raises(session, test_user, manicure_service):
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)
    booking = await svc.create_booking(test_user.id, manicure_service.id, start)
    await session.commit()

    await svc.cancel_booking(booking.id, now=dt.datetime(2026, 10, 1, tzinfo=RIGA_TZ))
    await session.commit()

    with pytest.raises(BookingNotCancellableError):
        await svc.cancel_booking(booking.id, now=dt.datetime(2026, 10, 1, tzinfo=RIGA_TZ))


@pytest.mark.asyncio
async def test_reschedule_success_moves_booking(session, test_user, manicure_service):
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    old_start = dt.datetime.combine(NEXT_FRIDAY, dt.time(11, 0), tzinfo=RIGA_TZ)
    new_start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)

    old_booking = await svc.create_booking(test_user.id, manicure_service.id, old_start)
    await session.commit()

    new_booking = await svc.reschedule_booking(
        old_booking.id, new_start, now=dt.datetime(2026, 10, 1, tzinfo=RIGA_TZ)
    )
    await session.commit()

    assert new_booking.previous_booking_id == old_booking.id
    old_refetched = await svc.bookings.get_by_id(old_booking.id)
    assert old_refetched.status == BookingStatus.CANCELLED.value
    assert new_booking.status == BookingStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_reschedule_keeps_old_booking_if_new_slot_taken(
    session, test_user, test_user_2, manicure_service
):
    """Критично: если новый слот занят другим клиентом, старая запись НЕ должна теряться."""
    await _setup_working_hours(session, NEXT_FRIDAY.weekday())
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    old_start = dt.datetime.combine(NEXT_FRIDAY, dt.time(11, 0), tzinfo=RIGA_TZ)
    conflicting_start = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)

    old_booking = await svc.create_booking(test_user.id, manicure_service.id, old_start)
    await session.commit()
    # другой клиент уже занял 15:00
    await svc.create_booking(test_user_2.id, manicure_service.id, conflicting_start)
    await session.commit()

    with pytest.raises(SlotNotAvailableError):
        await svc.reschedule_booking(
            old_booking.id, conflicting_start, now=dt.datetime(2026, 10, 1, tzinfo=RIGA_TZ)
        )

    # старая запись жива и всё ещё confirmed
    still_there = await svc.bookings.get_by_id(old_booking.id)
    assert still_there.status == BookingStatus.CONFIRMED.value
