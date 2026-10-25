"""Этап 11, «тест на тесты»: набор изоляции реально ловит утечку, а не просто зелёный.

Для ключевых репозиториев метод подменяется (monkeypatch) на версию БЕЗ фильтра по master_id — ровно такую ошибку мог бы
допустить разработчик. Соответствующий случай из tests/test_stage11_repositories.py обязан упасть (AssertionError). Перед
подменой тот же случай прогоняется на настоящем коде и обязан пройти: красный результат — из-за подмены, а не из-за поломки.
"""

import pytest
from sqlalchemy import delete, func, select, update

from app.database.models import AdminReminder, Booking, BookingStatus, MasterClient, Review, Service, User
from app.database.repositories.admin_reminder_repository import AdminReminderRepository
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.service_repository import ServiceRepository
from app.database.repositories.user_repository import UserRepository
from tests.stage11_support import two_masters  # noqa: F401  (фикстура pytest)
from tests.test_stage11_repositories import CASES, run_case


# ---- «сломанные» версии методов: те же запросы, но без фильтра по мастеру ----


async def leaky_service_get_by_id(self, service_id):
    return (await self.session.execute(select(Service).where(Service.id == service_id))).scalar_one_or_none()


async def leaky_booking_get_by_id(self, booking_id):
    return (await self.session.execute(select(Booking).where(Booking.id == booking_id))).scalar_one_or_none()


async def leaky_booking_get_owned(self, booking_id, telegram_id):
    return (
        await self.session.execute(
            select(Booking).join(User, User.id == Booking.user_id).where(Booking.id == booking_id, User.telegram_id == telegram_id)
        )
    ).scalar_one_or_none()


async def leaky_count_active(self, user_id, now):
    return (
        await self.session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.user_id == user_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime >= now,
                Booking.manual_client_name.is_(None),
            )
        )
    ).scalar_one()


async def leaky_reviews_stats(self):
    average, count = (
        await self.session.execute(select(func.avg(Review.rating), func.count(Review.id)).where(Review.active.is_(True)))
    ).one()
    return (float(average), count)


async def leaky_active_reviews(self):
    return list((await self.session.execute(select(Review).where(Review.active.is_(True)))).scalars().all())


async def leaky_user_get_by_id(self, user_id):
    return (await self.session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def leaky_user_get_client(self, user_id):
    # Берём строку «последнего» мастера — как если бы забыли условие master_id.
    query = select(MasterClient).where(MasterClient.user_id == user_id).order_by(MasterClient.master_id.desc())
    return (await self.session.execute(query)).scalars().first()


async def leaky_reminder_delete(self, reminder_id):
    result = await self.session.execute(delete(AdminReminder).where(AdminReminder.id == reminder_id))
    return result.rowcount > 0


async def silent_reminder_advance(self, reminder_id, fired_at, next_fire_at):
    """Утечка, которую не видно по ответу: строка B меняется, а метод честно возвращает False. Ловит только снимок B."""
    await self.session.execute(
        update(AdminReminder).where(AdminReminder.id == reminder_id, AdminReminder.next_fire_at == fired_at).values(next_fire_at=next_fire_at)
    )
    return False


LEAKS = {
    ("ServiceRepository", "get_by_id"): (ServiceRepository, leaky_service_get_by_id),
    ("BookingRepository", "get_by_id"): (BookingRepository, leaky_booking_get_by_id),
    ("BookingRepository", "get_owned_by_telegram_id"): (BookingRepository, leaky_booking_get_owned),
    ("BookingRepository", "count_active_for_user"): (BookingRepository, leaky_count_active),
    ("ContentRepository", "get_reviews_stats"): (ContentRepository, leaky_reviews_stats),
    ("ContentRepository", "get_active_reviews"): (ContentRepository, leaky_active_reviews),
    ("UserRepository", "get_by_id"): (UserRepository, leaky_user_get_by_id),
    ("UserRepository", "get_client"): (UserRepository, leaky_user_get_client),
    ("AdminReminderRepository", "delete"): (AdminReminderRepository, leaky_reminder_delete),
    ("AdminReminderRepository", "advance"): (AdminReminderRepository, silent_reminder_advance),
}


def test_leaks_target_real_cases():
    assert set(LEAKS) <= set(CASES)
    assert len({cls for cls, _ in LEAKS.values()}) >= 5  # не 2–3 репозитория, а больше: страховка шире минимума


@pytest.mark.parametrize("key", sorted(LEAKS), ids=lambda key: f"{key[0]}.{key[1]}")
async def test_isolation_suite_fails_when_a_repository_loses_its_master_filter(session, two_masters, monkeypatch, key):
    repo_class, leaky = LEAKS[key]
    await run_case(session, two_masters, key)  # настоящий код — зелёный

    monkeypatch.setattr(repo_class, key[1], leaky)
    with pytest.raises(AssertionError):
        await run_case(session, two_masters, key)  # без фильтра — красный: утечка поймана
