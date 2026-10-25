"""Этап 11, уровень БД: изоляция держится на самой PostgreSQL, а не только на коде приложения.

- список таблиц с master_id собирается из metadata, таблицы без него перечислены явно с обоснованием (NON_TENANT_TABLES);
- фикстура «два мастера» обязана заполнять КАЖДУЮ таблицу с master_id (новая таблица без данных в фикстуре роняет тест);
- вставка без master_id невозможна; составные внешние ключи не пускают услугу/запись другого мастера;
- одинаковое время у разных мастеров можно, пересечение у одного — нельзя (exclusion constraint);
- удаление мастера каскадно удаляет только его строки (счётчики во ВСЕХ таблицах с master_id до и после).

Известное ограничение схемы (не утечка): у `bookings.user_id` нет составного ключа на master_clients — `users` это глобальная
личность. Клиентом мастера человек становится через UserRepository; запись с чужим клиентом через интерфейс создать нельзя
(user_id всегда берётся из апдейта или из записи мастера), но БД такую строку сама не отклонит.
"""

import datetime as dt

import pytest
from sqlalchemy import delete, func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database.models import Base, Booking, BookingStatus, Master, Notification, User
from tests.stage11_support import (
    A,
    B,
    NON_TENANT_TABLES,
    PRICE,
    TAG,
    row_counts,
    snapshot,
    start_at,
    tenant_tables,
    two_masters,  # noqa: F401  (фикстура pytest)
)

NOW = dt.datetime(2030, 1, 7, 12, tzinfo=dt.timezone.utc)


def valid_values(ns) -> dict[str, dict]:
    """Минимально допустимые значения для вставки в каждую таблицу данных мастера — БЕЗ master_id."""
    day = ns.day
    return {
        "admin_reminders": dict(text="t", recurrence="once", anchor_date=day, remind_time=dt.time(9), next_fire_at=NOW),
        "blocked_periods": dict(start_datetime=start_at(day, 6), end_datetime=start_at(day, 7)),
        "booking_limit_requests": dict(user_id=ns.only_id[A], message="m"),
        "bookings": dict(
            user_id=ns.shared_id,
            service_id=ns.service_id[A],
            start_datetime=start_at(day, 8),
            end_datetime=start_at(day, 9),
            status="confirmed",
            service_name_snapshot="s",
            service_price_snapshot=1,
            service_duration_snapshot=60,
        ),
        "breaks": dict(weekday=1, start_time=dt.time(12), end_time=dt.time(13)),
        "client_access_requests": dict(user_id=ns.only_id[A], message="m"),
        "master_bots": dict(telegram_bot_id=999001, bot_token_encrypted="x", webhook_secret="no-master-secret"),
        "master_clients": dict(user_id=ns.only_id[A]),
        "master_deferred_messages": dict(text="t"),
        "master_quiet_mode": dict(start_time=dt.time(22), end_time=dt.time(8)),
        "master_profile": dict(name="n"),
        "master_settings": dict(),
        "notifications": dict(booking_id=ns.booking_id[A], scheduled_at=NOW),
        "portfolio_items": dict(telegram_file_id="f"),
        "reviews": dict(text="t", rating=5),
        "services": dict(name="n", price=1, duration_minutes=30),
        "working_day_overrides": dict(day=day, is_working=False),
        "working_hours": dict(weekday=1, is_working=False),
    }


# ---------------------------------------------------------------- решение по каждой таблице


def test_every_table_is_either_tenant_or_explicitly_not():
    """Новая таблица без master_id и без записи в NON_TENANT_TABLES (с обоснованием) — красный тест."""
    all_tables = set(Base.metadata.tables)
    tenant = {t.name for t in tenant_tables()}
    undecided = all_tables - tenant - set(NON_TENANT_TABLES)
    assert not undecided, (
        f"Таблицы без master_id и без решения: {sorted(undecided)}. Добавьте master_id (данные мастера) или запись "
        "в NON_TENANT_TABLES (tests/stage11_support.py) с объяснением."
    )
    assert not (tenant & set(NON_TENANT_TABLES)), "таблица с master_id не может быть в списке «без master_id»"
    assert set(NON_TENANT_TABLES) <= all_tables, "в NON_TENANT_TABLES есть несуществующие таблицы"
    assert all(len(reason) > 30 for reason in NON_TENANT_TABLES.values()), "у каждой таблицы без master_id нужно обоснование"
    assert len(tenant) >= 15


def test_values_for_insert_check_cover_every_tenant_table(two_masters):
    assert set(valid_values(two_masters)) == {t.name for t in tenant_tables()}


async def test_database_tables_match_metadata(session):
    rows = await session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
    )
    in_db = {name for (name,) in rows.all()} - {"alembic_version"}
    assert in_db == set(Base.metadata.tables), "таблицы БД и модели разошлись (alembic upgrade head / alembic check)"


async def test_fixture_fills_every_tenant_table_for_both_masters(session, two_masters):
    """Фикстура «два мастера» полна: утечку нельзя пропустить из-за пустой таблицы."""
    for master_id in (A, B):
        empty = [name for name, count in (await row_counts(session, master_id)).items() if count == 0]
        assert not empty, f"у мастера {master_id} в фикстуре нет строк в таблицах {empty}"


# ---------------------------------------------------------------- master_id обязателен


async def test_master_id_is_not_null_in_the_database_for_every_tenant_table(session):
    rows = await session.execute(
        text("SELECT table_name, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'master_id'")
    )
    in_db = dict(rows.all())
    assert set(in_db) == {t.name for t in tenant_tables()}
    assert {name for name, nullable in in_db.items() if nullable != "NO"} == set()


async def test_master_id_references_masters_with_cascade_for_every_tenant_table(session):
    connection = await session.connection()

    def foreign_keys(sync_conn):
        inspector = inspect(sync_conn)
        return {t.name: inspector.get_foreign_keys(t.name) for t in tenant_tables()}

    fks = await connection.run_sync(foreign_keys)
    for table, keys in fks.items():
        match = [
            fk for fk in keys if fk["constrained_columns"] == ["master_id"] and fk["referred_table"] == "masters"
        ]
        assert match, f"{table}: нет внешнего ключа master_id → masters.id"
        assert match[0]["options"].get("ondelete") == "CASCADE", f"{table}: ключ master_id без ON DELETE CASCADE"


@pytest.mark.parametrize("table", [t.name for t in tenant_tables()])
async def test_insert_without_master_id_is_impossible(session, two_masters, table):
    values = valid_values(two_masters)[table]
    metadata_table = Base.metadata.tables[table]
    with pytest.raises(IntegrityError) as error:
        await session.execute(insert(metadata_table).values(**values))
    assert "master_id" in str(error.value), "вставка отклонена не из-за master_id: проверьте минимальные значения в valid_values"
    await session.rollback()


async def test_insert_with_valid_values_and_master_id_works(session, two_masters):
    """Контроль для теста выше: те же значения С master_id проходят — значит, падали именно из-за его отсутствия."""
    values = valid_values(two_masters)
    for table in ("services", "reviews", "portfolio_items", "blocked_periods", "admin_reminders"):
        await session.execute(insert(Base.metadata.tables[table]).values(master_id=A, **values[table]))
    await session.commit()


# ---------------------------------------------------------------- составные ключи


def _booking(master_id, user_id, service_id, start, end, status=BookingStatus.CONFIRMED.value) -> Booking:
    return Booking(
        master_id=master_id,
        user_id=user_id,
        service_id=service_id,
        start_datetime=start,
        end_datetime=end,
        status=status,
        service_name_snapshot="s",
        service_price_snapshot=1,
        service_duration_snapshot=60,
    )


async def test_booking_of_a_with_service_of_b_is_rejected_by_composite_fk(session, two_masters):
    ns = two_masters
    session.add(_booking(A, ns.shared_id, ns.service_id[B], start_at(ns.day, 8), start_at(ns.day, 9)))
    with pytest.raises(IntegrityError) as error:
        await session.flush()
    assert "fk_bookings_master_service" in str(error.value)


async def test_notification_of_a_with_booking_of_b_is_rejected_by_composite_fk(session, two_masters):
    ns = two_masters
    session.add(Notification(master_id=A, booking_id=ns.booking2_id[B], scheduled_at=NOW))
    with pytest.raises(IntegrityError) as error:
        await session.flush()
    assert "fk_notifications_master_booking" in str(error.value)


async def test_own_service_and_own_booking_pass_the_composite_fks(session, two_masters):
    ns = two_masters
    session.add(_booking(A, ns.shared_id, ns.service_id[A], start_at(ns.day, 8), start_at(ns.day, 9)))
    session.add(Notification(master_id=A, booking_id=ns.booking2_id[A], scheduled_at=NOW))
    await session.flush()


# ---------------------------------------------------------------- время: разрешено у разных, запрещено у одного


async def test_same_time_at_two_masters_is_allowed(session, two_masters):
    ns = two_masters
    owners = (
        await session.execute(select(Booking.master_id).where(Booking.start_datetime == start_at(ns.day, 12)).order_by(Booking.master_id))
    ).scalars().all()
    assert owners == [A, B]  # фикстура уже записала обоих на 12:00 — теперь ещё одну пару на новое время
    session.add_all(
        [
            _booking(A, ns.shared_id, ns.service_id[A], start_at(ns.day, 8), start_at(ns.day, 9)),
            _booking(B, ns.shared_id, ns.service_id[B], start_at(ns.day, 8), start_at(ns.day, 9)),
        ]
    )
    await session.flush()


@pytest.mark.parametrize("start_h,start_m,end_h,end_m", [(12, 30, 13, 30), (11, 30, 12, 30), (12, 0, 13, 0), (11, 0, 14, 0)])
async def test_overlap_at_one_master_is_rejected_by_exclusion_constraint(session, two_masters, start_h, start_m, end_h, end_m):
    ns = two_masters
    session.add(_booking(A, ns.only_id[A], ns.service_id[A], start_at(ns.day, start_h, start_m), start_at(ns.day, end_h, end_m)))
    with pytest.raises(IntegrityError) as error:
        await session.flush()
    assert "ex_bookings_no_overlap_confirmed" in str(error.value)


async def test_adjacent_and_cancelled_bookings_do_not_conflict(session, two_masters):
    ns = two_masters
    # Встык с 12:00–13:00 (диапазон [)), и пересечение отменённой записью — не конфликт.
    session.add(_booking(A, ns.only_id[A], ns.service_id[A], start_at(ns.day, 13), start_at(ns.day, 14)))
    session.add(
        _booking(A, ns.only_id[A], ns.service_id[A], start_at(ns.day, 12, 15), start_at(ns.day, 12, 45), BookingStatus.CANCELLED.value)
    )
    await session.flush()


# ---------------------------------------------------------------- каскад


async def test_deleting_master_b_removes_only_its_rows_in_every_table(session, two_masters):
    ns = two_masters
    before_a = await snapshot(session, A)
    counts_b = await row_counts(session, B)
    assert all(count > 0 for count in counts_b.values()), counts_b  # есть что удалять в каждой таблице
    users_before = (await session.execute(select(func.count()).select_from(User))).scalar_one()

    await session.execute(delete(Master).where(Master.id == B))
    await session.commit()

    assert await snapshot(session, A) == before_a  # у мастера A не изменилось ни одной строки ни в одной таблице
    assert set((await row_counts(session, B)).values()) == {0}  # у B не осталось ничего
    assert (await session.execute(select(Master.id).where(Master.id == B))).first() is None
    assert (await session.execute(select(func.count()).select_from(User))).scalar_one() == users_before  # люди глобальны


# Удаление мастера №1 здесь намеренно не проверяется: он создаётся миграцией, и фикстуры tests/conftest.py на нём держатся.


async def test_tags_make_the_two_masters_distinguishable(session, two_masters):
    """Контроль фикстуры: в БД лежат данные ДВУХ разных мастеров, а не копия одного."""
    names = (await session.execute(select(Booking.service_name_snapshot).distinct().order_by(Booking.service_name_snapshot))).scalars().all()
    assert names == [f"Услуга-{TAG[A]}", f"Услуга-{TAG[B]}"]
    assert PRICE[A] != PRICE[B]
