import asyncio
import datetime as dt
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.database.models import Booking, BookingStatus, Notification, NotificationStatus, WorkingHours
from app.database.session import make_session_factory
from app.services.booking_service import BookingService
from app.utils.invite import INVITE_START_PREFIX, build_invite_link, build_share_url, new_invite_token
from tests.conftest import MASTER_ID, TEST_DATABASE_URL
from tests.conftest import next_weekday

RIGA_TZ = dt.timezone(dt.timedelta(hours=2))
NEXT_FRIDAY = next_weekday(4)  # пятница через 2+ недели: дата всегда в будущем
START = dt.datetime.combine(NEXT_FRIDAY, dt.time(15, 0), tzinfo=RIGA_TZ)
BEFORE_START = START - dt.timedelta(days=1)


async def _invited_booking(session, owner, service, token: str = "tok123") -> Booking:
    """Запись, которую мастер создал вручную: числится за «владельцем» (мастером) и несёт токен приглашения."""
    session.add(
        WorkingHours(master_id=MASTER_ID,
            weekday=NEXT_FRIDAY.weekday(), is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0)
        )
    )
    await session.commit()
    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(
        owner.id, service.id, START, manual_client_name="Анна", manual_client_contact="+375291234567"
    )
    booking.invite_token = token
    await session.commit()
    return booking


async def _notification_count(session, booking_id: int, status: str) -> int:
    result = await session.execute(
        select(func.count(Notification.id)).where(Notification.booking_id == booking_id, Notification.status == status)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_claim_binds_booking_to_client_and_is_one_time(session, test_user, test_user_2, manicure_service):
    booking = await _invited_booking(session, test_user, manicure_service)
    pending_before = await _notification_count(session, booking.id, NotificationStatus.PENDING.value)
    assert pending_before > 0
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)

    claimed = await svc.claim_invitation("tok123", test_user_2.id, now=BEFORE_START)
    await session.commit()

    assert claimed is not None and claimed.id == booking.id
    assert claimed.user_id == test_user_2.id
    assert claimed.invite_token is None
    # данные, которые ввёл мастер, и напоминания остаются на месте — теперь они идут клиенту
    assert claimed.manual_client_name == "Анна"
    assert await _notification_count(session, booking.id, NotificationStatus.PENDING.value) == pending_before

    # ссылка одноразовая: повторное открытие (другим человеком или тем же) ничего не привязывает
    assert await svc.claim_invitation("tok123", test_user.id, now=BEFORE_START) is None
    assert (await svc.bookings.get_by_id(booking.id)).user_id == test_user_2.id


@pytest.mark.asyncio
async def test_claim_unknown_token_returns_none(session, test_user, test_user_2, manicure_service):
    await _invited_booking(session, test_user, manicure_service)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)

    assert await svc.claim_invitation("nope", test_user_2.id, now=BEFORE_START) is None


@pytest.mark.asyncio
async def test_claim_rejects_cancelled_booking(session, test_user, test_user_2, manicure_service):
    booking = await _invited_booking(session, test_user, manicure_service)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    await svc.cancel_booking(booking.id)
    await session.commit()

    assert await svc.claim_invitation("tok123", test_user_2.id, now=BEFORE_START) is None
    refreshed = await svc.bookings.get_by_id(booking.id)
    assert refreshed.user_id == test_user.id
    assert refreshed.status == BookingStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_claim_rejects_past_booking(session, test_user, test_user_2, manicure_service):
    booking = await _invited_booking(session, test_user, manicure_service)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)

    assert await svc.claim_invitation("tok123", test_user_2.id, now=START + dt.timedelta(hours=1)) is None
    assert (await svc.bookings.get_by_id(booking.id)).user_id == test_user.id


@pytest.mark.asyncio
async def test_concurrent_claim_only_one_client_wins(session, test_user, test_user_2, manicure_service):
    """Ссылку, отправленную в общий чат, открывают двое сразу — запись достаётся только одному."""
    booking = await _invited_booking(session, test_user, manicure_service)
    session_factory = make_session_factory(TEST_DATABASE_URL)

    async def attempt(user_id: int):
        async with session_factory() as s:
            claimed = await BookingService(s, RIGA_TZ, master_id=MASTER_ID).claim_invitation("tok123", user_id, now=BEFORE_START)
            await s.commit()
            return user_id if claimed is not None else None

    results = await asyncio.gather(attempt(test_user_2.id), attempt(test_user.id))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    async with session_factory() as s:
        assert (await s.get(Booking, booking.id)).user_id == winners[0]


@pytest.mark.asyncio
async def test_reschedule_carries_pending_invite_to_new_booking(session, test_user, test_user_2, manicure_service):
    """Мастер перенёс запись, пока клиент не принял приглашение: ссылка должна вести на новую запись."""
    booking = await _invited_booking(session, test_user, manicure_service)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    new_start = dt.datetime.combine(NEXT_FRIDAY, dt.time(17, 0), tzinfo=RIGA_TZ)

    new_booking = await svc.reschedule_booking(booking.id, new_start)
    await session.commit()

    assert new_booking.invite_token == "tok123"
    assert (await svc.bookings.get_by_id(booking.id)).invite_token is None

    claimed = await svc.claim_invitation("tok123", test_user_2.id, now=BEFORE_START)
    assert claimed is not None and claimed.id == new_booking.id
    assert claimed.start_datetime == new_start


@pytest.mark.asyncio
async def test_reschedule_without_invite_leaves_token_empty(session, test_user, manicure_service):
    booking = await _invited_booking(session, test_user, manicure_service, token=None)
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    new_start = dt.datetime.combine(NEXT_FRIDAY, dt.time(17, 0), tzinfo=RIGA_TZ)

    new_booking = await svc.reschedule_booking(booking.id, new_start)
    await session.commit()

    assert new_booking.invite_token is None


def test_invite_link_and_share_url():
    token = new_invite_token()
    assert 0 < len(token) <= 64
    assert all(c.isalnum() or c in "_-" for c in token)  # допустимый для /start параметр Telegram
    assert new_invite_token() != token

    link = build_invite_link("my_beauty_bot", token)
    assert link == f"https://t.me/my_beauty_bot?start={INVITE_START_PREFIX}{token}"

    text = "Вы записаны: Маникюр — 16.10.2026 в 15:00 & ещё?"
    share = build_share_url(link, text)
    params = parse_qs(urlparse(share).query)
    assert urlparse(share).netloc == "t.me" and urlparse(share).path == "/share/url"
    assert params["url"] == [link]  # ссылка не ломается из-за ? и = внутри
    assert params["text"] == [text]
