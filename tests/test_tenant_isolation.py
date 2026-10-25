"""Изоляция мастеров на уровне репозиториев и сервисов: мастер A не видит и не меняет данные мастера B.

Два мастера (№1 из миграции и №2), у каждого свои услуги, записи, клиенты, отзывы, портфолио, расписание
и напоминания. Один Telegram-пользователь (`both`) — клиент обоих мастеров.
"""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.admin import client_access
from app.bot.handlers.client import my_booking
from app.bot.keyboards.callback_data import AdminClientAccessCallback, BookingActionCallback
from app.database.models import (
    AdminReminder,
    BlockedPeriod,
    Booking,
    BookingLimitRequest,
    BookingStatus,
    BreakPeriod,
    ClientAccessRequest,
    LimitRequestStatus,
    Master,
    MasterClient,
    MasterProfile,
    Notification,
    NotificationStatus,
    NotificationType,
    PortfolioItem,
    Recurrence,
    Review,
    Service,
    User,
    WorkingHours,
)
from app.database.repositories.admin_reminder_repository import AdminReminderRepository
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.schedule_repository import ScheduleRepository
from app.database.repositories.service_repository import ServiceRepository
from app.database.repositories.user_repository import UserRepository
from app.services.access_request_service import (
    create_access_request,
    decide_access_request,
    get_access_request,
    get_pending_access_request,
)
from app.services.admin_reminder_service import AdminReminderService
from app.services.booking_service import BookingService, complete_past_bookings
from app.services.exceptions import (
    BookingLimitReachedError,
    BookingNotFoundError,
    ClientBlockedError,
    ClientRestrictedError,
    ServiceNotFoundError,
)
from app.services.limit_request_service import (
    create_request,
    decide_request,
    get_pending_request,
    get_request,
    get_unused_approval,
)
from app.services.reminder_service import ReminderService
from app.services.report_service import build_report_workbook
from app.services.stats_service import PERIOD_WEEK, get_period_stats
from app.tenancy import TenantContext
from tests.conftest import RIGA_TZ, client_row, make_client, next_weekday, set_client, set_owner
from tests.tg import tg_harness

A, B = 1, 2
ADMIN_ID = 999


@pytest_asyncio.fixture
async def w(session):
    """Два мастера с данными. `w.a_*` — мастера №1, `w.b_*` — мастера №2."""
    await set_owner(session, ADMIN_ID)
    session.add(Master(id=B, owner_telegram_id=777))
    await session.flush()

    day = next_weekday(0)  # понедельник
    start = dt.datetime.combine(day, dt.time(12, 0), tzinfo=RIGA_TZ)
    ns = SimpleNamespace(day=day, start=start)

    for master_id in (A, B):
        session.add(MasterProfile(master_id=master_id, name=f"Мастер {master_id}", timezone="Europe/Riga"))
        session.add(WorkingHours(master_id=master_id, weekday=0, is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
        session.add(BreakPeriod(master_id=master_id, weekday=0, start_time=dt.time(15), end_time=dt.time(16)))
    ns.a_service = Service(master_id=A, name="Услуга A", price=40, duration_minutes=60)
    ns.b_service = Service(master_id=B, name="Услуга B", price=70, duration_minutes=60)
    session.add_all([ns.a_service, ns.b_service])

    ns.a_only = await make_client(session, 111111, A, username="only_a", first_name="OnlyA")
    ns.b_only = await make_client(session, 222222, B, username="only_b", first_name="OnlyB")
    ns.both = await make_client(session, 333333, A, username="both", first_name="Both")
    await make_client(session, 333333, B)
    await session.flush()

    # Записи в одно и то же время у разных мастеров — допустимо.
    ns.a_booking = await BookingService(session, RIGA_TZ, master_id=A).create_booking(ns.a_only.id, ns.a_service.id, start)
    ns.b_booking = await BookingService(session, RIGA_TZ, master_id=B).create_booking(ns.b_only.id, ns.b_service.id, start)

    session.add_all(
        [
            Review(master_id=A, client_id=ns.a_only.id, author_name="a", text="отзыв A", rating=5),
            Review(master_id=B, client_id=ns.b_only.id, author_name="b", text="отзыв B", rating=1),
            PortfolioItem(master_id=A, telegram_file_id="fa", title="A"),
            PortfolioItem(master_id=B, telegram_file_id="fb", title="B"),
            BlockedPeriod(
                master_id=B,
                start_datetime=start + dt.timedelta(hours=4),
                end_datetime=start + dt.timedelta(hours=5),
                reason="b",
            ),
            AdminReminder(
                master_id=B,
                text="напоминание B",
                recurrence=Recurrence.ONCE.value,
                anchor_date=day,
                remind_time=dt.time(9),
                next_fire_at=start - dt.timedelta(days=1),
            ),
            AdminReminder(
                master_id=A,
                text="напоминание A",
                recurrence=Recurrence.ONCE.value,
                anchor_date=day,
                remind_time=dt.time(9),
                next_fire_at=start - dt.timedelta(days=1),
            ),
        ]
    )
    await session.commit()
    # Обычные числа: после session.expire_all() обращаться к атрибутам ORM-объектов в async-сессии нельзя.
    for name in ("a_booking", "b_booking", "a_service", "b_service", "a_only", "b_only", "both"):
        setattr(ns, f"{name}_id", getattr(ns, name).id)
    for name in ("a_only", "b_only", "both"):
        setattr(ns, f"{name}_tg", getattr(ns, name).telegram_id)
    return ns


def at(w, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(w.day, dt.time(hour, minute), tzinfo=RIGA_TZ)


# --- репозитории: чтение ---


async def test_services_are_per_master(session, w):
    a, b = ServiceRepository(session, A), ServiceRepository(session, B)
    assert [s.name for s in await a.get_all()] == ["Услуга A"]
    assert [s.name for s in await b.get_active()] == ["Услуга B"]
    assert await a.get_by_id(w.b_service_id) is None
    assert (await b.get_by_id(w.b_service_id)).id == w.b_service_id


async def test_bookings_are_per_master(session, w):
    a, b = BookingRepository(session, A), BookingRepository(session, B)
    lo, hi = at(w, 0), at(w, 23)
    assert [x.id for x in await a.get_confirmed_in_range(lo, hi)] == [w.a_booking_id]
    assert [x.id for x in await b.get_confirmed_busy_in_range(lo, hi)] == [w.b_booking_id]
    assert [x.id for x in await a.get_confirmed_upcoming(lo)] == [w.a_booking_id]
    assert await a.get_by_id(w.b_booking_id) is None
    assert await a.get_owned_by_telegram_id(w.b_booking_id, w.b_only_tg) is None
    assert (await b.get_owned_by_telegram_id(w.b_booking_id, w.b_only_tg)).id == w.b_booking_id


async def test_booking_counters_and_lists_ignore_other_masters(session, w):
    now = at(w, 0)
    a = BookingRepository(session, A)
    assert await a.count_active_for_user(w.a_only_id, now) == 1
    assert await a.count_active_for_user(w.b_only_id, now) == 0  # чужой клиент: у мастера A записей нет
    assert await a.get_user_upcoming(w.b_only_id, now) == []
    assert await a.get_recent_self_bookings(w.b_only_id, None, 10) == []
    assert await a.count_completed_for_user(w.b_only_id) == 0


async def test_content_is_per_master(session, w):
    a, b = ContentRepository(session, A), ContentRepository(session, B)
    assert (await a.get_master_profile()).name == "Мастер 1"
    assert (await b.get_master_profile()).name == "Мастер 2"
    assert [i.title for i in await a.get_active_portfolio()] == ["A"]
    assert [r.text for r in await b.get_active_reviews()] == ["отзыв B"]
    assert await a.get_reviews_stats() == (5.0, 1)
    assert await b.get_reviews_stats() == (1.0, 1)
    assert [r.text for r in await a.get_reviews_page(0, 10)] == ["отзыв A"]
    b_review = (await b.get_active_reviews())[0]
    b_item = (await b.get_active_portfolio())[0]
    assert await a.get_review_by_id(b_review.id) is None
    assert await a.get_portfolio_item(b_item.id) is None
    assert await a.get_review_by_client_id(w.b_only_id) is None


async def test_schedule_is_per_master(session, w):
    a, b = ScheduleRepository(session, A), ScheduleRepository(session, B)
    lo, hi = at(w, 0), at(w, 23)
    assert await a.get_blocked_periods(lo, hi) == []
    blocks = await b.get_blocked_periods(lo, hi)
    assert len(blocks) == 1
    assert await a.get_blocked_period(blocks[0].id) is None
    assert await a.get_upcoming_blocked_periods(lo, 10) == []
    assert len(await b.get_upcoming_blocked_periods(lo, 10)) == 1
    assert len(await a.get_breaks(0)) == 1 and len(await b.get_breaks(0)) == 1


async def test_admin_reminders_are_per_master(session, w):
    a, b = AdminReminderRepository(session, A), AdminReminderRepository(session, B)
    far_future = at(w, 0) + dt.timedelta(days=365)
    assert [r.text for r in await a.list_upcoming(10)] == ["напоминание A"]
    assert await a.count() == 1 and await b.count() == 1
    assert [r.text for r in await b.get_due(far_future)] == ["напоминание B"]
    b_reminder = (await b.list_upcoming(1))[0]
    assert await a.get(b_reminder.id) is None


async def test_clients_are_per_master(session, w):
    a, b = UserRepository(session, A), UserRepository(session, B)
    assert await a.get_by_telegram_id(w.b_only_tg) is None
    assert await a.get_by_id(w.b_only_id) is None
    assert await a.get_by_username("only_b") is None
    assert await a.get_client(w.b_only_id) is None
    assert (await b.get_by_username("ONLY_B")).id == w.b_only_id
    assert (await a.get_by_id(w.both_id)).id == (await b.get_by_id(w.both_id)).id == w.both_id


# --- репозитории: изменения ---


async def test_other_master_cannot_delete_or_advance_reminder(session, w):
    a = AdminReminderRepository(session, A)
    b_reminder = (await AdminReminderRepository(session, B).list_upcoming(1))[0]
    fired_at = b_reminder.next_fire_at
    assert await a.delete(b_reminder.id) is False
    assert await a.advance(b_reminder.id, fired_at, fired_at + dt.timedelta(days=1)) is False
    assert await a.delete_if_fired_at(b_reminder.id, fired_at) is False
    assert await AdminReminderRepository(session, B).count() == 1


async def test_other_master_cannot_delete_blocked_period(session, w):
    b_block = (await ScheduleRepository(session, B).get_upcoming_blocked_periods(at(w, 0), 1))[0]
    with pytest.raises(ValueError):
        await ScheduleRepository(session, A).delete_blocked_period(b_block)
    assert await ScheduleRepository(session, B).get_blocked_period(b_block.id) is not None


async def test_working_hours_upsert_touches_only_own_master(session, w):
    await ScheduleRepository(session, A).upsert_working_hours(0, False, None, None)
    await session.commit()
    assert (await ScheduleRepository(session, A).get_working_hours(0)).is_working is False
    assert (await ScheduleRepository(session, B).get_working_hours(0)).is_working is True


async def test_new_rows_get_the_repository_master(session, w):
    breaks = ScheduleRepository(session, B)
    br = await breaks.add_break(1, dt.time(12), dt.time(13))
    block = await breaks.add_blocked_period(at(w, 10), at(w, 11), None)
    reminder = await AdminReminderRepository(session, B).add("x", "once", w.day, dt.time(9), at(w, 9))
    assert (br.master_id, block.master_id, reminder.master_id) == (B, B, B)
    created = BookingRepository(session, B).create(
        user_id=w.b_only_id,
        service_id=w.b_service_id,
        start_datetime=at(w, 17),
        end_datetime=at(w, 18),
        service_name_snapshot="x",
        service_price_snapshot=1,
        service_duration_snapshot=60,
    )
    assert created.master_id == B


async def test_invite_token_cannot_be_claimed_through_another_master(session, w):
    session.add(
        Booking(
            master_id=A,
            user_id=w.a_only_id,
            service_id=w.a_service_id,
            start_datetime=at(w, 17),
            end_datetime=at(w, 18),
            service_name_snapshot="x",
            service_price_snapshot=1,
            service_duration_snapshot=60,
            invite_token="tok-a",
        )
    )
    await session.commit()
    now = at(w, 0)
    assert await BookingRepository(session, B).claim_by_invite_token("tok-a", w.b_only_id, now) is None
    session.expire_all()
    token_booking = (await session.execute(select(Booking).where(Booking.invite_token == "tok-a"))).scalar_one()
    assert token_booking.user_id == w.a_only_id
    assert (await BookingRepository(session, A).claim_by_invite_token("tok-a", w.both_id, now)).user_id == w.both_id


# --- UserRepository: один человек — клиент двух мастеров ---


async def test_get_or_create_links_existing_person_to_new_master_without_duplicate(session, w):
    user = await UserRepository(session, B).get_or_create(w.a_only_tg, "only_a", "OnlyA", None)
    assert user.id == w.a_only_id
    links = (await session.execute(select(MasterClient).where(MasterClient.user_id == user.id))).scalars().all()
    assert sorted(link.master_id for link in links) == [A, B]
    assert len((await session.execute(select(User).where(User.telegram_id == user.telegram_id))).scalars().all()) == 1
    # повторный /start ничего не дублирует
    await UserRepository(session, B).get_or_create(w.a_only_tg, "only_a", "OnlyA", None)
    assert len((await session.execute(select(MasterClient).where(MasterClient.user_id == user.id))).scalars().all()) == 2


async def test_person_can_have_different_restrictions_at_two_masters(session, w):
    await set_client(session, w.both_id, A, is_restricted=True)
    with pytest.raises(ClientRestrictedError):
        await BookingService(session, RIGA_TZ, master_id=A).ensure_client_can_book(w.both_id)
    assert await BookingService(session, RIGA_TZ, master_id=B).ensure_client_can_book(w.both_id) is None

    await set_client(session, w.both_id, B, is_blocked_forever=True)
    with pytest.raises(ClientBlockedError):
        await BookingService(session, RIGA_TZ, master_id=B).ensure_client_can_book(w.both_id)
    assert (await client_row(session, w.both_id, A)).is_blocked_forever is False


# --- BookingService ---


async def test_same_time_at_two_masters_is_allowed_and_each_sees_own_slots(session, w):
    assert w.a_booking.start_datetime == w.b_booking.start_datetime
    for master_id in (A, B):
        slots = await BookingService(session, RIGA_TZ, master_id=master_id).get_available_slots(w.day, 60)
        assert w.start not in slots  # свой мастер занят в 12:00 своей записью
    blocked_b = await BookingService(session, RIGA_TZ, master_id=B).get_available_slots(w.day, 60)
    free_a = await BookingService(session, RIGA_TZ, master_id=A).get_available_slots(w.day, 60)
    assert at(w, 14) in free_a and at(w, 14) in blocked_b
    assert at(w, 16) in free_a and at(w, 16) not in blocked_b  # 16:00 закрыт блокировкой только у B


async def test_booking_at_a_time_taken_only_at_other_master_succeeds(session, w):
    # у A занято в 12:00 — запись клиента B-мастера на 13:00 у B не зависит от A
    booking = await BookingService(session, RIGA_TZ, master_id=B).create_booking(w.both_id, w.b_service_id, at(w, 13))
    assert booking.master_id == B


async def test_service_of_other_master_cannot_be_booked(session, w):
    with pytest.raises(ServiceNotFoundError):
        await BookingService(session, RIGA_TZ, master_id=A).create_booking(w.a_only_id, w.b_service_id, at(w, 17))


async def test_cancel_and_reschedule_of_foreign_booking_fail_and_change_nothing(session, w):
    svc = BookingService(session, RIGA_TZ, master_id=A)
    with pytest.raises(BookingNotFoundError):
        await svc.cancel_booking(w.b_booking_id, now=at(w, 0))
    with pytest.raises(BookingNotFoundError):
        await svc.reschedule_booking(w.b_booking_id, at(w, 17), now=at(w, 0))
    session.expire_all()
    assert (await session.get(Booking, w.b_booking_id)).status == BookingStatus.CONFIRMED.value


async def test_cancel_all_upcoming_touches_only_own_bookings(session, w):
    # `both` записан у обоих мастеров
    await BookingService(session, RIGA_TZ, master_id=A).create_booking(w.both_id, w.a_service_id, at(w, 13))
    b_second = await BookingService(session, RIGA_TZ, master_id=B).create_booking(w.both_id, w.b_service_id, at(w, 13))
    b_second_id = b_second.id
    await session.commit()
    cancelled = await BookingService(session, RIGA_TZ, master_id=A).cancel_all_upcoming(w.both_id, now=at(w, 0))
    assert [b.master_id for b in cancelled] == [A]
    await session.commit()
    session.expire_all()
    assert (await session.get(Booking, b_second_id)).status == BookingStatus.CONFIRMED.value


async def test_cancel_creates_and_cancels_notifications_only_of_own_master(session, w):
    notifications = (await session.execute(select(Notification))).scalars().all()
    assert {n.master_id for n in notifications} == {A, B}
    await BookingService(session, RIGA_TZ, master_id=A).cancel_booking(w.a_booking_id, now=at(w, 0))
    await session.commit()
    session.expire_all()
    by_master = {}
    for n in (await session.execute(select(Notification))).scalars():
        by_master.setdefault(n.master_id, set()).add(n.status)
    assert by_master[A] == {NotificationStatus.CANCELLED.value}
    assert by_master[B] == {NotificationStatus.PENDING.value}


async def test_booking_limit_is_counted_per_master(session, w):
    """Три записи у мастера A не мешают клиенту записаться к мастеру B."""
    svc_a = BookingService(session, RIGA_TZ, master_id=A)
    for hour in (10, 13, 14):
        await svc_a.create_booking(w.both_id, w.a_service_id, at(w, hour), enforce_client_limits=True)
    with pytest.raises(BookingLimitReachedError):
        await svc_a.create_booking(w.both_id, w.a_service_id, at(w, 17), enforce_client_limits=True)
    booking_b = await BookingService(session, RIGA_TZ, master_id=B).create_booking(
        w.both_id, w.b_service_id, at(w, 14), enforce_client_limits=True
    )
    assert booking_b.master_id == B


async def test_complete_past_bookings_only_for_own_master(session, w):
    after = at(w, 20)
    assert await complete_past_bookings(session, A, after) == 1
    session.expire_all()
    assert (await session.get(Booking, w.a_booking_id)).status == BookingStatus.COMPLETED.value
    assert (await session.get(Booking, w.b_booking_id)).status == BookingStatus.CONFIRMED.value


# --- ReminderService / AdminReminderService ---


async def test_reminder_service_sends_only_own_notifications(session, w):
    sent: list[int] = []

    async def send(booking: Booking) -> None:
        sent.append(booking.id)

    far = w.start  # все 24-часовые напоминания уже наступили
    count = await ReminderService(session, send, master_id=A).process_due_notifications(now=far)
    assert count == 3 and set(sent) == {w.a_booking_id}  # три напоминания (за сутки и два за 2 часа) — только записи A
    session.expire_all()
    b_statuses = {
        n.status for n in (await session.execute(select(Notification).where(Notification.master_id == B))).scalars()
    }
    assert NotificationStatus.SENT.value not in b_statuses


async def test_admin_reminders_are_sent_only_for_own_master(session, w):
    texts: list[str] = []

    async def send(text: str) -> None:
        texts.append(text)

    sent = await AdminReminderService(session, RIGA_TZ, master_id=A).process_due(send, now=w.start)
    assert sent == 1 and texts == ["напоминание A"]
    assert await AdminReminderRepository(session, B).count() == 1


# --- stats / report ---


async def test_stats_and_report_count_only_own_master(session, w):
    now = at(w, 9)
    stats_a, _, _ = await get_period_stats(session, A, RIGA_TZ, PERIOD_WEEK, now)
    stats_b, _, _ = await get_period_stats(session, B, RIGA_TZ, PERIOD_WEEK, now)
    assert (stats_a.bookings_count, int(stats_a.expected_revenue)) == (1, 40)
    assert (stats_b.bookings_count, int(stats_b.expected_revenue)) == (1, 70)

    buffer, _ = await build_report_workbook(session, A, RIGA_TZ, PERIOD_WEEK, now)
    from openpyxl import load_workbook

    ws = load_workbook(buffer).active
    names = {str(c.value) for row in ws.iter_rows(min_row=2) for c in row}
    assert "Услуга A" in names and "Услуга B" not in names


# --- заявки клиента мастеру ---


async def test_limit_requests_are_per_master(session, w):
    request_a = await create_request(session, A, w.both_id, "к A")
    request_b = await create_request(session, B, w.both_id, "к B")  # у другого мастера ожидающий запрос не мешает
    assert request_a is not None and request_b is not None
    request_a_id, request_b_id = request_a.id, request_b.id
    await session.commit()
    assert (await get_pending_request(session, A, w.both_id)).id == request_a_id
    assert await create_request(session, A, w.both_id, "ещё раз") is None

    assert await get_request(session, A, request_b_id) is None
    assert await decide_request(session, A, request_b_id, True) is None  # чужое решение не применяется
    await session.commit()
    session.expire_all()
    assert (await session.get(BookingLimitRequest, request_b_id)).status == LimitRequestStatus.PENDING.value

    assert (await decide_request(session, B, request_b_id, True)).status == LimitRequestStatus.APPROVED.value
    await session.commit()
    assert await get_unused_approval(session, B, w.both_id) is not None
    assert await get_unused_approval(session, A, w.both_id) is None


async def test_access_requests_and_decisions_are_per_master(session, w):
    await set_client(session, w.both_id, A, is_restricted=True)
    await set_client(session, w.both_id, B, is_restricted=True)
    req_a = await create_access_request(session, A, w.both_id, "к A")
    req_b = await create_access_request(session, B, w.both_id, "к B")
    assert req_a is not None and req_b is not None
    req_a_id, req_b_id = req_a.id, req_b.id
    await session.commit()
    assert await get_access_request(session, A, req_b_id) is None
    assert await decide_access_request(session, A, req_b_id, True) is None  # через мастера A чужое обращение не решить

    decided = await decide_access_request(session, A, req_a_id, True)  # открыли доступ только у A
    assert decided is not None
    await session.commit()
    assert (await client_row(session, w.both_id, A)).is_restricted is False
    assert (await client_row(session, w.both_id, B)).is_restricted is True
    assert await get_pending_access_request(session, A, w.both_id) is None
    assert (await get_pending_access_request(session, B, w.both_id)).id == req_b_id


async def test_access_request_unique_pending_is_per_master_in_db(session, w):
    session.add_all(
        [
            ClientAccessRequest(master_id=A, user_id=w.both_id, message="1"),
            ClientAccessRequest(master_id=B, user_id=w.both_id, message="2"),
        ]
    )
    await session.flush()


# --- хендлеры: подделанные callback_data ---


async def test_client_cannot_cancel_booking_of_another_master_via_handler(session, w, monkeypatch):
    async with tg_harness(my_booking.router, master_id=A) as h:
        await h.press(w.b_only_tg, BookingActionCallback(booking_id=w.b_booking_id, action="cancel_confirm"))
        assert h.tg.alerts() == ["Запись не найдена."]
    session.expire_all()
    assert (await session.get(Booking, w.b_booking_id)).status == BookingStatus.CONFIRMED.value


async def test_master_cannot_restrict_client_of_another_master_via_handler(session, w):
    async with tg_harness(client_access.router, master_id=A) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=w.b_only_id, action="restrict"))
        assert h.tg.alerts() == ["Клиент не найден."]
    assert (await client_row(session, w.b_only_id, B)).is_restricted is False
    assert (await session.get(Booking, w.b_booking_id)).status == BookingStatus.CONFIRMED.value


async def test_tenant_middleware_gives_handler_the_resolved_master(session, w):
    from aiogram import Router
    from aiogram.types import Message

    seen: list[TenantContext] = []
    router = Router(name="probe")

    @router.message()
    async def probe(message: Message, tenant: TenantContext):
        seen.append(tenant)

    async with tg_harness(router, master_id=B) as h:
        await h.send_text(ADMIN_ID, "hi")
    assert [(t.master_id, t.master.id) for t in seen] == [(B, B)]


async def test_inactive_master_updates_are_dropped(session, w):
    from aiogram import Router
    from aiogram.types import Message

    seen: list[int] = []
    router = Router(name="probe2")

    @router.message()
    async def probe(message: Message):
        seen.append(1)

    (await session.get(Master, B)).is_active = False
    await session.commit()
    async with tg_harness(router, master_id=B) as h:
        await h.send_text(ADMIN_ID, "hi")
    assert seen == []
