"""Этап 11, уровень репозиториев: ВСЕ подклассы MasterScopedRepository находятся интроспекцией, и для каждого публичного
метода есть проверка «репозиторий мастера A не видит и не меняет ничего у мастера B».

Как это устроено
- `discover_methods()` собирает (класс, метод) по всем подклассам из пакета app.database.repositories. Новый репозиторий или
  новый публичный метод без записи в CASES / MANUAL_SKIP роняет `test_every_public_method_is_covered`.
- Каждый случай из CASES — async-функция `(repo_A, ns)`: вызывает метод с id/значениями мастера B (и со своими, чтобы ловить
  смешение) и сама проверяет результат. Раннер дополнительно проверяет (1) что в возвращённом нет строк мастера B и людей,
  известных только B, (2) что снимок ВСЕХ таблиц данных мастера B после вызова идентичен снимку до него.
- MANUAL_SKIP — методы, которые нельзя вызвать автоматически, с причиной. Молча пропускать нельзя: устаревшая запись тоже
  роняет тест.
"""

import datetime as dt
import importlib
import inspect
import pkgutil
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.database.repositories as repositories_package
from app.database.models import (
    BlockedPeriod,
    BookingStatus,
    MasterSettings,
    User,
    WorkingDayOverride,
    WorkingHours,
)
from app.database.repositories.admin_reminder_repository import AdminReminderRepository
from app.database.repositories.base import MasterScopedRepository
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.master_settings_repository import MasterSettingsRepository
from app.database.repositories.quiet_mode_repository import DeferredMessageRepository, QuietModeRepository
from app.database.repositories.schedule_repository import ScheduleRepository
from app.database.repositories.service_repository import ServiceRepository
from app.database.repositories.user_repository import UserRepository
from tests.stage11_support import (
    A,
    B,
    CURRENCY,
    INVITE_TOKEN,
    ONLY_TG,
    OWNER,
    SHARED_TG,
    TAG,
    snapshot,
    start_at,
    two_masters,  # noqa: F401  (фикстура pytest)
)

CaseFunc = Callable[[object, SimpleNamespace], Awaitable[object]]


# ---------------------------------------------------------------- интроспекция


def repository_classes() -> list[type[MasterScopedRepository]]:
    for module_info in pkgutil.iter_modules(repositories_package.__path__):
        importlib.import_module(f"{repositories_package.__name__}.{module_info.name}")
    found: set[type] = set()
    stack: list[type] = [MasterScopedRepository]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub.__module__.startswith("app.") and sub not in found:  # тестовые подклассы (сторожа) не в счёт
                found.add(sub)
                stack.append(sub)
    return sorted(found, key=lambda c: c.__name__)


def public_methods(cls: type) -> list[str]:
    base = set(dir(MasterScopedRepository))
    return sorted(
        name for name in dir(cls) if not name.startswith("_") and name not in base and callable(getattr(cls, name, None))
    )


def discover_methods() -> set[tuple[str, str]]:
    return {(cls.__name__, name) for cls in repository_classes() for name in public_methods(cls)}


# Методы, которые нельзя вызвать автоматически: (класс, метод) -> причина. Пока таких нет — все методы вызываются.
MANUAL_SKIP: dict[tuple[str, str], str] = {}

CASES: dict[tuple[str, str], CaseFunc] = {}


def case(cls_name: str, method: str):
    def register(func: CaseFunc) -> CaseFunc:
        CASES[(cls_name, method)] = func
        return func

    return register


def _ids(items) -> set[int]:
    return {item.id for item in items}


def rng(ns):
    return start_at(ns.day, 0), start_at(ns.day, 23, 59)


# ---------------------------------------------------------------- UserRepository


@case("UserRepository", "get_by_telegram_id")
async def _(repo: UserRepository, ns):
    assert await repo.get_by_telegram_id(ONLY_TG[B]) is None
    assert await repo.get_by_telegram_id(OWNER[B]) is None
    own = await repo.get_by_telegram_id(ONLY_TG[A])
    assert own is not None and own.id == ns.only_id[A]
    shared = await repo.get_by_telegram_id(SHARED_TG)
    assert shared is not None and shared.id == ns.shared_id  # общий человек — клиент и A тоже
    return [own, shared]


@case("UserRepository", "get_by_id")
async def _(repo: UserRepository, ns):
    assert await repo.get_by_id(ns.only_id[B]) is None
    assert await repo.get_by_id(ns.owner_id[B]) is None
    own = await repo.get_by_id(ns.only_id[A])
    assert own is not None and own.telegram_id == ONLY_TG[A]
    return own


@case("UserRepository", "get_client")
async def _(repo: UserRepository, ns):
    assert await repo.get_client(ns.only_id[B]) is None
    link = await repo.get_client(ns.shared_id)
    # У B общий клиент ограничен, у A — нет: флаги не смешиваются.
    assert link is not None and link.master_id == A and link.is_restricted is False
    return link


@case("UserRepository", "get_by_username")
async def _(repo: UserRepository, ns):
    assert await repo.get_by_username("only_b") is None
    assert await repo.get_by_username("OWNER_B") is None
    own = await repo.get_by_username("ONLY_A")
    assert own is not None and own.id == ns.only_id[A]
    return own


@case("UserRepository", "get_or_create")
async def _(repo: UserRepository, ns):
    # Человек, известный только мастеру B, пишет боту A: по замыслу он становится клиентом A (users — глобальная личность),
    # но связь с B и её флаги не меняются (это проверит снимок B), а у A он «чистый».
    user = await repo.get_or_create(ONLY_TG[B], "only_b", f"Only{TAG[B]}", None)
    assert user.id == ns.only_id[B]
    link = await repo.get_client(user.id)
    assert link is not None and link.master_id == A
    assert (link.is_restricted, link.is_blocked_forever, link.is_in_admin_panel) == (False, False, False)
    return None


@case("UserRepository", "set_admin_panel_mode")
async def _(repo: UserRepository, ns):
    # Владелец B в B «в админ-панели». Выключение режима у A не должно задеть его строку у B (снимок B ниже).
    await repo.set_admin_panel_mode(OWNER[B], False)
    assert await repo.is_in_admin_panel(OWNER[B]) is False
    await repo.set_admin_panel_mode(OWNER[A], True)
    assert await repo.is_in_admin_panel(OWNER[A]) is True
    return None


@case("UserRepository", "is_in_admin_panel")
async def _(repo: UserRepository, ns):
    assert await repo.is_in_admin_panel(OWNER[B]) is False  # у B владелец в панели, у A он вообще не клиент
    assert await repo.is_in_admin_panel(OWNER[A]) is True
    assert await repo.is_in_admin_panel(ONLY_TG[B]) is False
    return None


# ---------------------------------------------------------------- BookingRepository


def _a_confirmed(ns) -> set[int]:
    return {ns.booking_id[A], ns.booking2_id[A], ns.manual_booking_id[A]}


@case("BookingRepository", "get_confirmed_in_range")
async def _(repo: BookingRepository, ns):
    items = await repo.get_confirmed_in_range(*rng(ns))
    assert _ids(items) == _a_confirmed(ns)
    return items


@case("BookingRepository", "get_confirmed_busy_in_range")
async def _(repo: BookingRepository, ns):
    items = await repo.get_confirmed_busy_in_range(*rng(ns))
    assert _ids(items) == _a_confirmed(ns)
    return items


@case("BookingRepository", "get_confirmed_upcoming")
async def _(repo: BookingRepository, ns):
    items = await repo.get_confirmed_upcoming(ns.now)
    assert _ids(items) == _a_confirmed(ns)
    return items


@case("BookingRepository", "count_active_for_user")
async def _(repo: BookingRepository, ns):
    assert await repo.count_active_for_user(ns.shared_id, ns.now) == 1  # у B у него тоже запись: сложилось бы 2
    assert await repo.count_active_for_user(ns.only_id[B], ns.now) == 0
    assert await repo.count_active_for_user(ns.only_id[A], ns.now) == 1
    return None


@case("BookingRepository", "count_master_created_active_for_user")
async def _(repo: BookingRepository, ns):
    assert await repo.count_master_created_active_for_user(ns.owner_id[A], ns.now) == 1
    assert await repo.count_master_created_active_for_user(ns.owner_id[B], ns.now) == 0
    return None


@case("BookingRepository", "count_completed_for_user")
async def _(repo: BookingRepository, ns):
    assert await repo.count_completed_for_user(ns.shared_id) == 1  # у B тоже одна выполненная: сложилось бы 2
    assert await repo.count_completed_for_user(ns.only_id[B]) == 0
    return None


@case("BookingRepository", "get_recent_self_bookings")
async def _(repo: BookingRepository, ns):
    items = await repo.get_recent_self_bookings(ns.shared_id, None, 10)
    assert _ids(items) == {ns.booking_id[A], ns.completed_booking_id[A]}
    assert await repo.get_recent_self_bookings(ns.only_id[B], None, 10) == []
    return items


@case("BookingRepository", "get_by_id")
async def _(repo: BookingRepository, ns):
    for foreign in (ns.booking_id[B], ns.booking2_id[B], ns.manual_booking_id[B], ns.completed_booking_id[B]):
        assert await repo.get_by_id(foreign) is None
    own = await repo.get_by_id(ns.booking_id[A])
    assert own is not None and own.master_id == A
    return own


@case("BookingRepository", "get_owned_by_telegram_id")
async def _(repo: BookingRepository, ns):
    # SHARED — настоящий владелец записи B, но репозиторий A её не отдаёт.
    assert await repo.get_owned_by_telegram_id(ns.booking_id[B], SHARED_TG) is None
    assert await repo.get_owned_by_telegram_id(ns.booking2_id[B], ONLY_TG[B]) is None
    own = await repo.get_owned_by_telegram_id(ns.booking_id[A], SHARED_TG)
    assert own is not None and own.id == ns.booking_id[A]
    return own


@case("BookingRepository", "get_user_upcoming")
async def _(repo: BookingRepository, ns):
    items = await repo.get_user_upcoming(ns.shared_id, ns.now)
    assert _ids(items) == {ns.booking_id[A]}  # запись того же человека у B не видна
    assert await repo.get_user_upcoming(ns.only_id[B], ns.now) == []
    return items


@case("BookingRepository", "claim_by_invite_token")
async def _(repo: BookingRepository, ns):
    # Токен-приглашение B нельзя «забрать» через A (снимок B ниже проверит, что запись B не привязалась).
    assert await repo.claim_by_invite_token(INVITE_TOKEN[B], ns.only_id[A], ns.now) is None
    return None


@case("BookingRepository", "create")
async def _(repo: BookingRepository, ns):
    # Запись мастера A с услугой мастера B отклоняется составным внешним ключом уже при flush.
    repo.create(
        user_id=ns.shared_id,
        service_id=ns.service_id[B],
        start_datetime=start_at(ns.day, 17),
        end_datetime=start_at(ns.day, 18),
        status=BookingStatus.CONFIRMED.value,
        service_name_snapshot="чужая услуга",
        service_price_snapshot=1,
        service_duration_snapshot=60,
    )
    with pytest.raises(IntegrityError):
        await repo.session.flush()
    await repo.session.rollback()
    # Своя услуга — запись создаётся у A.
    created = repo.create(
        user_id=ns.shared_id,
        service_id=ns.service_id[A],
        start_datetime=start_at(ns.day, 17),
        end_datetime=start_at(ns.day, 18),
        status=BookingStatus.CONFIRMED.value,
        service_name_snapshot="своя",
        service_price_snapshot=1,
        service_duration_snapshot=60,
    )
    await repo.session.flush()
    assert created.master_id == A
    return None


# ---------------------------------------------------------------- ContentRepository


@case("ContentRepository", "get_master_profile")
async def _(repo: ContentRepository, ns):
    profile = await repo.get_master_profile()
    assert profile is not None and profile.master_id == A and profile.address == f"Адрес {TAG[A]}"
    return profile


@case("ContentRepository", "get_active_portfolio")
async def _(repo: ContentRepository, ns):
    items = await repo.get_active_portfolio()
    assert _ids(items) == set(ns.portfolio_ids[A])
    assert await repo.get_active_portfolio(f"cat-{TAG[B]}") == []  # категория B у A пуста
    assert _ids(await repo.get_active_portfolio(f"cat-{TAG[A]}")) == set(ns.portfolio_ids[A])
    return items


@case("ContentRepository", "get_portfolio_item")
async def _(repo: ContentRepository, ns):
    for foreign in ns.portfolio_ids[B]:
        assert await repo.get_portfolio_item(foreign) is None
    own = await repo.get_portfolio_item(ns.portfolio_ids[A][0])
    assert own is not None and own.master_id == A
    return own


@case("ContentRepository", "get_active_reviews")
async def _(repo: ContentRepository, ns):
    items = await repo.get_active_reviews()
    assert _ids(items) == set(ns.review_ids[A])
    return items


@case("ContentRepository", "get_reviews_stats")
async def _(repo: ContentRepository, ns):
    average, count = await repo.get_reviews_stats()
    assert (average, count) == (4.5, 2)  # у B (2 и 1) среднее было бы 3.0 при смешении и 4 отзыва
    return None


@case("ContentRepository", "get_reviews_page")
async def _(repo: ContentRepository, ns):
    items = await repo.get_reviews_page(0, 10)
    assert _ids(items) == set(ns.review_ids[A])
    assert await repo.get_reviews_page(2, 10) == []  # у A всего два отзыва
    return items


@case("ContentRepository", "get_review_by_client_id")
async def _(repo: ContentRepository, ns):
    review = await repo.get_review_by_client_id(ns.shared_id)
    assert review is not None and review.master_id == A and review.rating == 5  # а не отзыв того же человека у B (рейтинг 2)
    assert await repo.get_review_by_client_id(ns.only_id[B]) is None
    return review


@case("ContentRepository", "get_review_by_id")
async def _(repo: ContentRepository, ns):
    for foreign in ns.review_ids[B]:
        assert await repo.get_review_by_id(foreign) is None
    own = await repo.get_review_by_id(ns.review_ids[A][0])
    assert own is not None and own.master_id == A
    return own


# ---------------------------------------------------------------- ScheduleRepository


@case("ScheduleRepository", "get_working_hours")
async def _(repo: ScheduleRepository, ns):
    wh = await repo.get_working_hours(0)
    assert wh is not None and wh.master_id == A and wh.start_time == dt.time(10)  # у B рабочий день с 9:00
    return wh


@case("ScheduleRepository", "get_breaks")
async def _(repo: ScheduleRepository, ns):
    items = await repo.get_breaks(0)
    assert _ids(items) == {ns.break_id[A]} and items[0].start_time == dt.time(15)
    return items


@case("ScheduleRepository", "get_all_working_hours")
async def _(repo: ScheduleRepository, ns):
    by_weekday = await repo.get_all_working_hours()
    assert by_weekday[0].master_id == A and by_weekday[0].start_time == dt.time(10)  # у B рабочий день с 9:00
    return list(by_weekday.values())


@case("ScheduleRepository", "get_all_breaks")
async def _(repo: ScheduleRepository, ns):
    by_weekday = await repo.get_all_breaks()
    assert _ids(by_weekday[0]) == {ns.break_id[A]}
    return [item for items in by_weekday.values() for item in items]


@case("ScheduleRepository", "get_blocked_periods")
async def _(repo: ScheduleRepository, ns):
    items = await repo.get_blocked_periods(*rng(ns))
    assert _ids(items) == {ns.block_id[A]}
    return items


@case("ScheduleRepository", "get_upcoming_blocked_periods")
async def _(repo: ScheduleRepository, ns):
    items = await repo.get_upcoming_blocked_periods(ns.now, 10)
    assert _ids(items) == {ns.block_id[A]}
    return items


@case("ScheduleRepository", "get_blocked_period")
async def _(repo: ScheduleRepository, ns):
    assert await repo.get_blocked_period(ns.block_id[B]) is None
    own = await repo.get_blocked_period(ns.block_id[A])
    assert own is not None and own.master_id == A
    return own


@case("ScheduleRepository", "delete_blocked_period")
async def _(repo: ScheduleRepository, ns):
    foreign = await repo.session.get(BlockedPeriod, ns.block_id[B])  # объект B в сессии A (как после подмены id)
    assert foreign is not None
    with pytest.raises(ValueError):
        await repo.delete_blocked_period(foreign)
    own = await repo.get_blocked_period(ns.block_id[A])
    await repo.delete_blocked_period(own)  # свою блокировку — можно
    assert await repo.get_blocked_period(ns.block_id[A]) is None
    return None


@case("ScheduleRepository", "upsert_working_hours")
async def _(repo: ScheduleRepository, ns):
    wh = await repo.upsert_working_hours(0, True, dt.time(8), dt.time(20))
    assert wh.master_id == A and wh.start_time == dt.time(8)
    rows = (await repo.session.execute(select(WorkingHours).where(WorkingHours.weekday == 0))).scalars().all()
    assert len(rows) == 2 and {r.master_id for r in rows} == {A, B}  # по строке на мастера, строка B не тронута
    return None


@case("ScheduleRepository", "get_override")
async def _(repo: ScheduleRepository, ns):
    own = await repo.get_override(ns.override_day)
    assert own is not None and own.id == ns.override_id[A] and own.start_time == dt.time(11)  # у B особый день с 12:00
    assert await repo.get_override(ns.override_day + dt.timedelta(days=1)) is None
    return own


@case("ScheduleRepository", "get_overrides_in_range")
async def _(repo: ScheduleRepository, ns):
    found = await repo.get_overrides_in_range(ns.override_day - dt.timedelta(days=1), ns.override_day + dt.timedelta(days=1))
    assert _ids(found.values()) == {ns.override_id[A]} and set(found) == {ns.override_day}
    return list(found.values())


@case("ScheduleRepository", "get_upcoming_overrides")
async def _(repo: ScheduleRepository, ns):
    items = await repo.get_upcoming_overrides(ns.override_day - dt.timedelta(days=1), 10)
    assert _ids(items) == {ns.override_id[A]}
    assert await repo.get_upcoming_overrides(ns.override_day + dt.timedelta(days=1), 10) == []
    return items


@case("ScheduleRepository", "get_working_hours_for_date")
async def _(repo: ScheduleRepository, ns):
    special = await repo.get_working_hours_for_date(ns.override_day)  # исключение главнее расписания недели
    assert isinstance(special, WorkingDayOverride) and special.master_id == A and special.start_time == dt.time(11)
    regular = await repo.get_working_hours_for_date(ns.override_day + dt.timedelta(days=1))  # без исключения — неделя
    assert regular is not None and regular.master_id == A and regular.start_time == dt.time(10)
    return [special, regular]


@case("ScheduleRepository", "set_override")
async def _(repo: ScheduleRepository, ns):
    changed = await repo.set_override(ns.override_day, False, dt.time(9), dt.time(10))  # закрытый день хранится без времени
    assert changed.master_id == A and changed.is_working is False and changed.start_time is None
    created = await repo.set_override(ns.override_day + dt.timedelta(days=2), True, dt.time(8), dt.time(12))
    assert created.master_id == A and created.start_time == dt.time(8)
    rows = (
        await repo.session.execute(select(WorkingDayOverride).where(WorkingDayOverride.day == ns.override_day))
    ).scalars().all()
    assert len(rows) == 2 and {r.master_id for r in rows} == {A, B}  # по строке на мастера, строка B не тронута
    return None


@case("ScheduleRepository", "delete_override")
async def _(repo: ScheduleRepository, ns):
    assert await repo.delete_override(ns.override_day) is True
    assert await repo.delete_override(ns.override_day) is False
    rows = (
        await repo.session.execute(select(WorkingDayOverride).where(WorkingDayOverride.day == ns.override_day))
    ).scalars().all()
    assert [r.master_id for r in rows] == [B]  # исключение B осталось
    return None


@case("ScheduleRepository", "add_break")
async def _(repo: ScheduleRepository, ns):
    brk = await repo.add_break(0, dt.time(18), dt.time(18, 30))
    assert brk.master_id == A
    assert len(await repo.get_breaks(0)) == 2
    return None


@case("ScheduleRepository", "set_lunch")
async def _(repo: ScheduleRepository, ns):
    lunch = await repo.set_lunch(0, dt.time(12), dt.time(13))
    assert lunch.master_id == A
    items = await repo.get_breaks(0)
    assert len(items) == 1 and items[0].start_time == dt.time(12)  # прежний перерыв A заменён, перерыв B не тронут
    return None


@case("ScheduleRepository", "clear_breaks")
async def _(repo: ScheduleRepository, ns):
    assert await repo.clear_breaks(0) == 1  # только перерыв A: перерыв B в подсчёт не попадает
    assert await repo.get_breaks(0) == []
    return None


@case("ScheduleRepository", "drop_breaks_outside_hours")
async def _(repo: ScheduleRepository, ns):
    assert await repo.drop_breaks_outside_hours(0, dt.time(8), dt.time(23)) == 0  # перерыв A (15:00) внутри часов
    assert await repo.drop_breaks_outside_hours(0, dt.time(8), dt.time(14)) == 1  # вне часов — удалён
    assert await repo.get_breaks(0) == []
    return None


@case("ScheduleRepository", "add_blocked_period")
async def _(repo: ScheduleRepository, ns):
    block = await repo.add_blocked_period(start_at(ns.day, 9), start_at(ns.day, 10), "новая")
    assert block.master_id == A
    assert len(await repo.get_blocked_periods(*rng(ns))) == 2
    return None


# ---------------------------------------------------------------- ServiceRepository


@case("ServiceRepository", "get_active")
async def _(repo: ServiceRepository, ns):
    items = await repo.get_active()
    assert _ids(items) == {ns.service_id[A]}
    return items


@case("ServiceRepository", "get_all")
async def _(repo: ServiceRepository, ns):
    items = await repo.get_all()
    assert _ids(items) == {ns.service_id[A], ns.inactive_service_id[A]}
    return items


@case("ServiceRepository", "get_by_id")
async def _(repo: ServiceRepository, ns):
    assert await repo.get_by_id(ns.service_id[B]) is None
    assert await repo.get_by_id(ns.inactive_service_id[B]) is None
    own = await repo.get_by_id(ns.inactive_service_id[A])
    assert own is not None and own.master_id == A
    return own


# ---------------------------------------------------------------- MasterSettingsRepository


@case("MasterSettingsRepository", "get_row")
async def _(repo: MasterSettingsRepository, ns):
    row = await repo.get_row()
    assert row is not None and row.master_id == A and row.currency == CURRENCY[A] and row.max_active_bookings == 3
    return row


@case("MasterSettingsRepository", "get_data")
async def _(repo: MasterSettingsRepository, ns):
    data = await repo.get_data()
    assert data.currency == CURRENCY[A] and data.max_active_bookings == 3  # у B — EUR и 5
    return None


@case("MasterSettingsRepository", "ensure_row")
async def _(repo: MasterSettingsRepository, ns):
    row = await repo.ensure_row()
    assert row.master_id == A and row.id == ns.settings_id[A]  # строка A уже есть, чужая не подхватывается
    return row


@case("MasterSettingsRepository", "update_fields")
async def _(repo: MasterSettingsRepository, ns):
    row = await repo.update_fields(currency="USD", max_active_bookings=7)
    assert row.master_id == A and row.currency == "USD"
    other = (await repo.session.execute(select(MasterSettings).where(MasterSettings.master_id == B))).scalar_one()
    assert other.currency == CURRENCY[B] and other.max_active_bookings == 5
    return None


@case("MasterSettingsRepository", "reset_fields")
async def _(repo: MasterSettingsRepository, ns):
    row = await repo.reset_fields()
    assert row.master_id == A and row.currency == "BYN" and row.max_active_bookings == 3
    return None


# ---------------------------------------------------------------- AdminReminderRepository


@case("AdminReminderRepository", "add")
async def _(repo: AdminReminderRepository, ns):
    reminder = await repo.add("новое", "once", ns.day, dt.time(10), ns.now + dt.timedelta(days=3))
    assert reminder.master_id == A
    assert await repo.count() == 2
    return None


@case("AdminReminderRepository", "get")
async def _(repo: AdminReminderRepository, ns):
    assert await repo.get(ns.reminder_id[B]) is None
    own = await repo.get(ns.reminder_id[A])
    assert own is not None and own.master_id == A
    return own


@case("AdminReminderRepository", "list_upcoming")
async def _(repo: AdminReminderRepository, ns):
    items = await repo.list_upcoming(10)
    assert _ids(items) == {ns.reminder_id[A]}
    return items


@case("AdminReminderRepository", "count")
async def _(repo: AdminReminderRepository, ns):
    assert await repo.count() == 1  # у B тоже одно: сложилось бы 2
    return None


@case("AdminReminderRepository", "get_due")
async def _(repo: AdminReminderRepository, ns):
    items = await repo.get_due(ns.now)
    assert _ids(items) == {ns.reminder_id[A]}
    return items


@case("AdminReminderRepository", "delete")
async def _(repo: AdminReminderRepository, ns):
    assert await repo.delete(ns.reminder_id[B]) is False
    return None


@case("AdminReminderRepository", "advance")
async def _(repo: AdminReminderRepository, ns):
    # Время срабатывания у обоих одинаковое — compare-and-set без фильтра по мастеру «сработал бы» на строке B.
    assert await repo.advance(ns.reminder_id[B], ns.fire_at, ns.fire_at + dt.timedelta(days=1)) is False
    return None


@case("AdminReminderRepository", "delete_if_fired_at")
async def _(repo: AdminReminderRepository, ns):
    assert await repo.delete_if_fired_at(ns.reminder_id[B], ns.fire_at) is False
    return None


# ---------------------------------------------------------------- QuietModeRepository


@case("QuietModeRepository", "get_row")
async def _(repo: QuietModeRepository, ns):
    row = await repo.get_row()
    assert row is not None and row.master_id == A and row.id == ns.quiet_id[A] and row.start_time == dt.time(22)
    return row


@case("QuietModeRepository", "get_data")
async def _(repo: QuietModeRepository, ns):
    data = await repo.get_data()
    assert data.enabled is False and data.start_time == dt.time(22) and data.end_time == dt.time(8)  # у B — 23:00–09:00
    return None


@case("QuietModeRepository", "save")
async def _(repo: QuietModeRepository, ns):
    await repo.save(enabled=True, start_time=dt.time(20), end_time=dt.time(6))
    row = await repo.get_row()
    assert row is not None and row.id == ns.quiet_id[A] and row.enabled is True and row.start_time == dt.time(20)
    return None


# ---------------------------------------------------------------- DeferredMessageRepository


@case("DeferredMessageRepository", "add")
async def _(repo: DeferredMessageRepository, ns):
    message = await repo.add("новое")
    assert message.master_id == A
    assert await repo.count() == 2
    return None


@case("DeferredMessageRepository", "count")
async def _(repo: DeferredMessageRepository, ns):
    assert await repo.count() == 1  # у B тоже одно: сложилось бы 2
    return None


@case("DeferredMessageRepository", "delete_expired")
async def _(repo: DeferredMessageRepository, ns):
    # Сообщения обоих мастеров просрочены одинаково: без фильтра по мастеру удалилось бы и сообщение B.
    assert await repo.delete_expired(ns.now) == 1
    assert await repo.count() == 0
    return None


@case("DeferredMessageRepository", "claim_oldest")
async def _(repo: DeferredMessageRepository, ns):
    message = await repo.claim_oldest()
    assert message is not None and message.master_id == A and message.id == ns.deferred_id[A]
    return message


@case("DeferredMessageRepository", "delete")
async def _(repo: DeferredMessageRepository, ns):
    await repo.delete(ns.deferred_id[B])  # чужое сообщение не удаляется (снимок B проверяет раннер)
    assert await repo.count() == 1
    await repo.delete(ns.deferred_id[A])
    assert await repo.count() == 0
    return None


# ---------------------------------------------------------------- проверки


def assert_no_foreign(value, ns, path: str = "result") -> None:
    """В возвращённом нет строк мастера B и людей, которых знает только B."""
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            assert_no_foreign(item, ns, f"{path}[{index}]")
        return
    master_id = getattr(value, "master_id", None)
    if isinstance(master_id, int):
        assert master_id == A, f"{path}: строка мастера {master_id} в ответе репозитория мастера {A}"
    if isinstance(value, User):
        assert value.id not in {ns.only_id[B]}, f"{path}: человек, известный только мастеру B"


def test_discovery_finds_all_known_repositories():
    """Интроспекция реально находит репозитории (иначе проверка ниже была бы пустой)."""
    names = {cls.__name__ for cls in repository_classes()}
    assert {
        "AdminReminderRepository",
        "BookingRepository",
        "ContentRepository",
        "MasterSettingsRepository",
        "ScheduleRepository",
        "ServiceRepository",
        "UserRepository",
    } <= names
    assert len(discover_methods()) >= 40


def test_every_public_method_is_covered():
    """Новый публичный метод репозитория без проверки изоляции (и без причины в MANUAL_SKIP) — красный тест."""
    found = discover_methods()
    uncovered = found - set(CASES) - set(MANUAL_SKIP)
    assert not uncovered, (
        f"Нет проверки изоляции для методов репозиториев: {sorted(uncovered)}. Добавьте @case(...) в "
        "tests/test_stage11_repositories.py или (с причиной!) запись в MANUAL_SKIP."
    )
    stale = (set(CASES) | set(MANUAL_SKIP)) - found
    assert not stale, f"Проверки для несуществующих методов (удалены/переименованы?): {sorted(stale)}"
    assert not (set(CASES) & set(MANUAL_SKIP)), "метод одновременно в CASES и MANUAL_SKIP"
    assert all(reason.strip() for reason in MANUAL_SKIP.values()), "у каждого пропуска должна быть причина"


async def run_case(session, ns, key: tuple[str, str]) -> None:
    """Один случай: вызов метода репозитория мастера A + проверка результата + снимок данных B до/после.

    Вынесено в функцию, чтобы тот же раннер использовал тест на тесты (tests/test_stage11_meta.py).
    """
    repo_class = next(cls for cls in repository_classes() if cls.__name__ == key[0])
    repo = repo_class(session, A)

    before_b = await snapshot(session, B)
    result = await CASES[key](repo, ns)
    await session.flush()
    assert_no_foreign(result, ns)
    await session.commit()
    after_b = await snapshot(session, B)

    assert after_b == before_b, f"{key[0]}.{key[1]}: вызов репозитория мастера A изменил данные мастера B"


@pytest.mark.parametrize("key", sorted(CASES), ids=lambda key: f"{key[0]}.{key[1]}")
async def test_repository_of_a_neither_returns_nor_changes_data_of_b(session, two_masters, key):
    await run_case(session, two_masters, key)


# ---------------------------------------------------------------- страховка: изоляция по master_id в обёрнутых репозиториях


def test_repository_requires_master_id():
    """Репозиторий без корректного master_id не создаётся (этап 5) — для каждого найденного класса."""
    from app.database.repositories.base import MasterIdRequiredError

    for cls in repository_classes():
        for bad in (None, 0, -1, True, "1"):
            with pytest.raises(MasterIdRequiredError):
                cls(object(), bad)  # type: ignore[arg-type]
