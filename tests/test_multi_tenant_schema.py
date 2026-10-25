"""Этап 1 multi-tenant: ограничения БД, на которых держится изоляция мастеров.

Это тесты схемы (миграции и модели), а не бизнес-логики. Изоляцию на уровне репозиториев и сервисов
(«мастер A не видит данные мастера B») проверяет tests/test_tenant_isolation.py.
"""

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Booking,
    ClientAccessRequest,
    Master,
    MasterClient,
    MasterProfile,
    Notification,
    Service,
    User,
    WorkingHours,
)
from app.database.repositories.user_repository import UserRepository

FIRST_MASTER_ID = 1
SECOND_MASTER_ID = 2
START = dt.datetime(2030, 1, 7, 12, 0, tzinfo=dt.timezone.utc)
END = START + dt.timedelta(hours=1)


@pytest_asyncio.fixture
async def second_master(session: AsyncSession) -> Master:
    master = Master(id=SECOND_MASTER_ID, owner_telegram_id=999)
    session.add(master)
    await session.flush()
    return master


async def _service(session: AsyncSession, master_id: int, name: str = "Маникюр") -> Service:
    service = Service(master_id=master_id, name=name, price=40, duration_minutes=60)
    session.add(service)
    await session.flush()
    return service


def _booking(master_id: int, user_id: int, service_id: int, start: dt.datetime = START, end: dt.datetime = END) -> Booking:
    return Booking(
        master_id=master_id,
        user_id=user_id,
        service_id=service_id,
        start_datetime=start,
        end_datetime=end,
        service_name_snapshot="Маникюр",
        service_price_snapshot=40,
        service_duration_snapshot=60,
    )


async def test_migration_creates_first_master(session):
    master = await session.get(Master, FIRST_MASTER_ID)
    assert master is not None
    assert master.is_active is True


@pytest.mark.parametrize(
    "model_factory",
    [
        lambda: Service(name="Без мастера", price=1, duration_minutes=30),
        lambda: MasterProfile(name="Без мастера"),
        lambda: WorkingHours(weekday=1, is_working=False),
        lambda: ClientAccessRequest(user_id=1, message="x"),
    ],
    ids=["service", "profile", "working_hours", "access_request"],
)
async def test_insert_without_master_id_fails(session, model_factory):
    """Переходного default больше нет: забытый master_id — ошибка БД, а не молчаливая запись мастеру №1."""
    session.add(model_factory())
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_master_clients_insert_without_master_id_fails(session, test_user):
    await session.execute(text("DELETE FROM master_clients"))
    session.add(MasterClient(user_id=test_user.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_new_user_becomes_client_of_first_master(session):
    user = await UserRepository(session, FIRST_MASTER_ID).get_or_create(
        telegram_id=555, username="u", first_name="U", last_name=None
    )
    links = (await session.execute(select(MasterClient).where(MasterClient.user_id == user.id))).scalars().all()
    assert [(link.master_id) for link in links] == [FIRST_MASTER_ID]


async def test_same_telegram_user_can_be_client_of_two_masters(session, second_master, test_user):
    """test_user уже клиент мастера №1 (фикстура) — добавляем его же второму мастеру."""
    session.add(MasterClient(master_id=SECOND_MASTER_ID, user_id=test_user.id))
    await session.flush()
    links = (await session.execute(select(MasterClient).where(MasterClient.user_id == test_user.id))).scalars().all()
    assert sorted(link.master_id for link in links) == [FIRST_MASTER_ID, SECOND_MASTER_ID]


async def test_client_link_is_unique_per_master(session, test_user):
    session.add(MasterClient(master_id=FIRST_MASTER_ID, user_id=test_user.id))  # такая связь уже есть из фикстуры
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_working_hours_weekday_is_unique_per_master_not_globally(session, second_master):
    for master_id in (FIRST_MASTER_ID, SECOND_MASTER_ID):
        session.add(
            WorkingHours(master_id=master_id, weekday=0, is_working=True, start_time=dt.time(10), end_time=dt.time(19))
        )
    await session.flush()


async def test_working_hours_weekday_is_unique_within_master(session):
    for _ in range(2):
        session.add(
            WorkingHours(master_id=FIRST_MASTER_ID, weekday=0, is_working=True, start_time=dt.time(10), end_time=dt.time(19))
        )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_profile_is_unique_per_master(session):
    session.add_all([MasterProfile(master_id=FIRST_MASTER_ID, name="A"), MasterProfile(master_id=FIRST_MASTER_ID, name="B")])
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_two_masters_can_have_bookings_at_the_same_time(session, second_master, test_user):
    first_service = await _service(session, FIRST_MASTER_ID)
    second_service = await _service(session, SECOND_MASTER_ID)
    session.add_all(
        [
            _booking(FIRST_MASTER_ID, test_user.id, first_service.id),
            _booking(SECOND_MASTER_ID, test_user.id, second_service.id),
        ]
    )
    await session.flush()


async def test_one_master_cannot_have_overlapping_confirmed_bookings(session, test_user, test_user_2):
    service = await _service(session, FIRST_MASTER_ID)
    session.add_all(
        [
            _booking(FIRST_MASTER_ID, test_user.id, service.id),
            _booking(FIRST_MASTER_ID, test_user_2.id, service.id, START + dt.timedelta(minutes=30), END + dt.timedelta(minutes=30)),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_booking_cannot_use_service_of_another_master(session, second_master, test_user):
    foreign_service = await _service(session, SECOND_MASTER_ID)
    session.add(_booking(FIRST_MASTER_ID, test_user.id, foreign_service.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_notification_cannot_use_booking_of_another_master(session, second_master, test_user):
    service = await _service(session, FIRST_MASTER_ID)
    booking = _booking(FIRST_MASTER_ID, test_user.id, service.id)
    session.add(booking)
    await session.flush()
    session.add(Notification(master_id=SECOND_MASTER_ID, booking_id=booking.id, scheduled_at=START))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_pending_access_request_is_unique_per_client_and_master(session, second_master, test_user):
    """Один клиент может ждать ответа сразу у двух мастеров, но не дважды у одного."""
    session.add_all(
        [
            ClientAccessRequest(master_id=FIRST_MASTER_ID, user_id=test_user.id, message="к первому"),
            ClientAccessRequest(master_id=SECOND_MASTER_ID, user_id=test_user.id, message="ко второму"),
        ]
    )
    await session.flush()

    session.add(ClientAccessRequest(master_id=FIRST_MASTER_ID, user_id=test_user.id, message="ещё раз к первому"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_master_deletes_only_its_data(session, second_master, test_user):
    first_service = await _service(session, FIRST_MASTER_ID)
    await _service(session, SECOND_MASTER_ID)
    await session.commit()

    await session.delete(second_master)
    await session.commit()

    remaining = (await session.execute(select(Service))).scalars().all()
    assert [s.id for s in remaining] == [first_service.id]
    assert await session.get(User, test_user.id) is not None
