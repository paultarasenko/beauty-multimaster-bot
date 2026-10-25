"""Этап 12C: пул БД, допуск апдейтов, охранник вызовов Bot API (429/403/401), advisory lock, миграция индексов,
пакетная проверка доступности. Тесты лёгкие; тесты с БД используют общую тестовую БД проекта (TEST_DATABASE_URL)."""

import asyncio
import datetime as dt
import types

import asyncpg
import httpx
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.methods import GetMe
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import event

from app.bot.dispatcher import build_dispatcher, default_routers
from app.config.settings import Settings, settings
from app.database.models import WorkingHours
from app.database.session import make_engine, pool_kwargs
from app.multibot.admission import AdmissionTimeout, UpdateAdmission
from app.multibot.call_guard import TelegramCallGuard
from app.multibot.crypto import TokenCipher
from app.multibot.registry import BotRegistry
from app.multibot.webhook import create_webhook_app
from app.scheduler.locks import lock_key, tick_lock
from app.services.booking_service import BookingService
from app.services.delivery import Outcome, classify_send_error, stop_from
from app.database.session import make_session_factory
from tests.conftest import MASTER_ID, RIGA_TZ, TEST_DATABASE_URL, next_weekday
from tests.test_migrations import _admin_dsn, _alembic, scratch_db_url  # noqa: F401  (фикстура)
from tests.test_multibot_registry import add_master, add_row
from tests.webhook_utils import FakeTelegram, message_update

# ------------------------------------------------------------------ пул соединений БД


def test_pool_defaults_are_safe(monkeypatch):
    for name in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW"):  # conftest уменьшает пул для набора тестов — проверяем настоящие умолчания
        monkeypatch.delenv(name, raising=False)
    fresh = Settings(_env_file=None)
    assert (fresh.db_pool_size, fresh.db_max_overflow, fresh.db_pool_timeout, fresh.db_pool_recycle) == (10, 15, 30.0, 1800)
    total_pool = fresh.db_pool_size + fresh.db_max_overflow
    assert total_pool <= 100 - 10, "пул по умолчанию должен помещаться в max_connections=100 с запасом"
    # очередь webhook меньше пула: ожидание случается ДО открытия сессии, а не в пуле
    assert fresh.webhook_max_concurrent_updates < total_pool
    assert fresh.webhook_bot_max_concurrency <= fresh.webhook_max_concurrent_updates
    assert fresh.webhook_queue_timeout_seconds < fresh.db_pool_timeout


@pytest.mark.parametrize("field", ["db_pool_size", "webhook_max_concurrent_updates", "webhook_bot_max_concurrency"])
def test_pool_and_limits_reject_zero(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_make_engine_uses_pool_settings(monkeypatch):
    monkeypatch.setattr(settings, "db_pool_size", 3)
    monkeypatch.setattr(settings, "db_max_overflow", 2)
    monkeypatch.setattr(settings, "db_pool_timeout", 7.5)
    monkeypatch.setattr(settings, "db_pool_recycle", 60)
    assert pool_kwargs() == {
        "pool_size": 3, "max_overflow": 2, "pool_timeout": 7.5, "pool_recycle": 60, "pool_pre_ping": True
    }
    engine = make_engine("postgresql+asyncpg://u:p@localhost:5432/x")
    pool = engine.sync_engine.pool
    assert (pool.size(), pool._max_overflow, pool._timeout, pool._recycle) == (3, 2, 7.5, 60)


# ------------------------------------------------------------------ допуск апдейтов


async def test_admission_slow_bot_does_not_block_others():
    admission = UpdateAdmission(max_total=6, max_per_bot=2, timeout_seconds=0.2)
    release = asyncio.Event()
    started = []

    async def slow(bot_id):
        async with admission.slot(bot_id):
            started.append(bot_id)
            await release.wait()

    tasks = [asyncio.create_task(slow(1)) for _ in range(2)]
    await asyncio.sleep(0.02)
    with pytest.raises(AdmissionTimeout):  # третий апдейт медленного бота не получает места...
        async with admission.slot(1):
            pytest.fail("лимит на бота превышен")
    async with admission.slot(2):  # ...а другой бот обслуживается сразу
        started.append(2)
    release.set()
    await asyncio.gather(*tasks)
    assert started.count(1) == 2 and started.count(2) == 1


async def test_admission_releases_slots_after_error_and_total_limit():
    admission = UpdateAdmission(max_total=1, max_per_bot=1, timeout_seconds=0.05)
    with pytest.raises(RuntimeError):
        async with admission.slot(1):
            raise RuntimeError("сбой обработки")
    async with admission.slot(1):  # место освободилось
        with pytest.raises(AdmissionTimeout):  # общий лимит = 1: другой бот ждёт и получает отказ
            async with admission.slot(2):
                pytest.fail("общий лимит превышен")
    async with admission.slot(2):  # неудачное ожидание не «утекло» из семафора бота 2
        pass


# ------------------------------------------------------------------ охранник вызовов Bot API: 429 / 403 / 401


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _bot(bot_id=111):
    return types.SimpleNamespace(id=bot_id)


async def test_guard_429_pauses_bot_without_calling_telegram_and_respects_retry_after():
    clock = Clock()
    guard = TelegramCallGuard(clock=clock)
    calls = []

    async def flood(bot, method):
        calls.append(bot.id)
        raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=40)

    with pytest.raises(TelegramRetryAfter):
        await guard(flood, _bot(), GetMe())
    assert calls == [111] and 39 < guard.paused_for(111) <= 40

    clock.now += 10  # пауза идёт: в Telegram не ходим, а ошибка несёт остаток паузы
    with pytest.raises(TelegramRetryAfter) as exc_info:
        await guard(flood, _bot(), GetMe())
    assert calls == [111] and 29 <= exc_info.value.retry_after <= 30
    await guard(_ok, _bot(222), GetMe())  # соседний бот не затронут

    clock.now += 31  # пауза кончилась — вызовы снова идут
    assert guard.paused_for(111) == 0
    assert await guard(_ok, _bot(), GetMe()) == "ok"


async def _ok(bot, method):
    return "ok"


async def test_guard_403_passes_through_and_does_not_pause():
    guard = TelegramCallGuard(clock=Clock())

    async def forbidden(bot, method):
        raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")

    for _ in range(3):
        with pytest.raises(TelegramForbiddenError):
            await guard(forbidden, _bot(), GetMe())
    assert guard.paused_for(111) == 0


async def test_guard_401_reports_once_per_interval_not_a_series():
    clock = Clock()
    guard = TelegramCallGuard(clock=clock, report_interval_seconds=3600)
    reported = []

    async def on_unauthorized(bot_id):
        reported.append(bot_id)

    guard.on_unauthorized = on_unauthorized

    async def unauthorized(bot, method):
        raise TelegramUnauthorizedError(method=method, message="Unauthorized")

    for _ in range(5):
        with pytest.raises(TelegramUnauthorizedError):
            await guard(unauthorized, _bot(), GetMe())
    assert reported == [111]
    with pytest.raises(TelegramUnauthorizedError):  # другой бот — отдельный сигнал
        await guard(unauthorized, _bot(222), GetMe())
    clock.now += 3601  # через интервал напомнить можно
    with pytest.raises(TelegramUnauthorizedError):
        await guard(unauthorized, _bot(), GetMe())
    assert reported == [111, 222, 111]


async def test_guard_callback_failure_does_not_mask_the_error():
    guard = TelegramCallGuard(clock=Clock())

    async def broken(_bot_id):
        raise RuntimeError("нет связи с БД")

    guard.on_unauthorized = broken

    async def unauthorized(bot, method):
        raise TelegramUnauthorizedError(method=method, message="Unauthorized")

    with pytest.raises(TelegramUnauthorizedError):
        await guard(unauthorized, _bot(), GetMe())


def test_delivery_classification_for_notifications():
    """Рассылка: 403 — уведомление failed без повторов (PERMANENT), 429 — стоп с retry_after, 401 — недействительный токен."""
    method = GetMe()
    assert classify_send_error(TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")) is Outcome.PERMANENT
    flood = TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=17)
    assert classify_send_error(flood) is Outcome.STOP_RETRY_AFTER and stop_from(flood, Outcome.STOP_RETRY_AFTER).retry_after == 17
    assert classify_send_error(TelegramUnauthorizedError(method=method, message="Unauthorized")) is Outcome.STOP_UNAUTHORIZED


# ------------------------------------------------------------------ advisory lock


@pytest_asyncio.fixture
async def two_factories():
    first, second = make_session_factory(TEST_DATABASE_URL), make_session_factory(TEST_DATABASE_URL)
    try:
        yield first, second
    finally:
        await first.kw["bind"].dispose()
        await second.kw["bind"].dispose()


def test_lock_key_is_stable_and_distinct_per_tick():
    names = ["notifications", "admin_reminders", "completion", "trial_digest"]
    keys = [lock_key(name) for name in names]
    assert keys == [lock_key(name) for name in names] and len(set(keys)) == 4
    assert all(0 <= key < 2**31 for key in keys)


async def test_advisory_lock_second_holder_skips_and_lock_is_released(two_factories):
    first, second = two_factories
    async with tick_lock(first, "notifications") as got_first:
        assert got_first is True
        async with tick_lock(second, "notifications") as got_second:
            assert got_second is False  # тот же тик в другом «процессе» пропускается
        async with tick_lock(second, "completion") as other_tick:
            assert other_tick is True  # другие тики друг другу не мешают
    async with tick_lock(second, "notifications") as after_release:
        assert after_release is True  # замок снят после выхода


async def test_advisory_lock_is_released_when_tick_raises(two_factories):
    first, second = two_factories
    with pytest.raises(RuntimeError):
        async with tick_lock(first, "notifications") as acquired:
            assert acquired
            raise RuntimeError("сбой внутри тика")
    async with tick_lock(second, "notifications") as acquired_again:
        assert acquired_again is True


async def test_advisory_lock_connection_is_not_left_in_transaction(two_factories):
    first, _ = two_factories
    async with tick_lock(first, "notifications"):
        conn = await asyncpg.connect(make_url_dsn())
        try:
            idle = await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction' AND datname = current_database()")
        finally:
            await conn.close()
        assert idle == 0


def make_url_dsn() -> str:
    from sqlalchemy.engine import make_url

    return make_url(TEST_DATABASE_URL).set(drivername="postgresql").render_as_string(hide_password=False)


# ------------------------------------------------------------------ миграция индексов


NEW_INDEXES = {"ix_bookings_master_user_start", "ix_bookings_master_end_confirmed", "ix_notifications_master_sending"}
OLD_INDEX = "ix_notifications_sending_claimed"
BEFORE_12C = "x4c0d6e2f8a9"


async def _index_names(db_name: str) -> set[str]:
    conn = await asyncpg.connect(_admin_dsn(db_name))
    try:
        rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        return {row["indexname"] for row in rows}
    finally:
        await conn.close()


def test_stage12c_migration_upgrade_downgrade_and_check(scratch_db_url):
    url, name = scratch_db_url
    assert _alembic(url, "upgrade", BEFORE_12C).returncode == 0
    before = asyncio.run(_index_names(name))
    assert OLD_INDEX in before and not (NEW_INDEXES & before)

    assert _alembic(url, "upgrade", "head").returncode == 0
    after = asyncio.run(_index_names(name))
    assert NEW_INDEXES <= after and OLD_INDEX not in after
    checked = _alembic(url, "check")
    assert checked.returncode == 0, checked.stdout + checked.stderr  # модели и миграции совпадают

    assert _alembic(url, "downgrade", BEFORE_12C).returncode == 0
    reverted = asyncio.run(_index_names(name))
    assert OLD_INDEX in reverted and not (NEW_INDEXES & reverted)
    assert _alembic(url, "upgrade", "head").returncode == 0  # повторный подъём


# ------------------------------------------------------------------ пакетная доступность


async def test_dates_with_slots_matches_per_date_has_any_slot_in_five_queries(session, test_user, manicure_service):
    """Пакетная проверка дат даёт тот же результат, что has_any_slot по каждой дате, но делает 5 запросов, а не ~120.

    5-й запрос — исключения из расписания на даты (особые дни): один пакетный запрос на весь диапазон."""
    today = dt.datetime.now(RIGA_TZ).date()
    for weekday in range(7):
        session.add(WorkingHours(master_id=MASTER_ID, weekday=weekday, is_working=weekday != 6, start_time=dt.time(10, 0), end_time=dt.time(13, 0)))
    await session.commit()
    svc = BookingService(session, RIGA_TZ, master_id=MASTER_ID)
    await svc.schedule.add_break(2, dt.time(11, 0), dt.time(12, 0))
    friday = next_weekday(4, 3)
    await svc.schedule.add_blocked_period(
        dt.datetime.combine(friday + dt.timedelta(days=1), dt.time(0, 0), tzinfo=RIGA_TZ),
        dt.datetime.combine(friday + dt.timedelta(days=2), dt.time(23, 59), tzinfo=RIGA_TZ),
        None,
    )
    await session.commit()
    # Дни, занятые целиком: одна запись на всё рабочее окно + «хвост» свободного времени.
    full_day = next_weekday(0, 3)
    manicure_service.duration_minutes = 180
    await session.commit()
    await svc.create_booking(test_user.id, manicure_service.id, dt.datetime.combine(full_day, dt.time(10, 0), tzinfo=RIGA_TZ))
    await session.commit()

    dates = [today + dt.timedelta(days=offset) for offset in range(30)]
    now = dt.datetime.now(RIGA_TZ)
    expected = [d for index, d in enumerate(dates) if await svc.has_any_slot(d, 60, now=now if index == 0 else None)]

    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    sync_engine = session.bind.sync_engine if hasattr(session.bind, "sync_engine") else session.get_bind()
    event.listen(sync_engine, "before_cursor_execute", count)
    try:
        actual = await svc.dates_with_slots(dates, 60, now=now)
    finally:
        event.remove(sync_engine, "before_cursor_execute", count)

    assert actual == expected
    assert full_day not in actual and friday + dt.timedelta(days=1) not in actual  # занятые и заблокированные даты отсеяны
    assert len(statements) <= 5, statements


async def test_dates_with_slots_empty_list(session):
    assert await BookingService(session, RIGA_TZ, master_id=MASTER_ID).dates_with_slots([], 60) == []


# ------------------------------------------------------------------ webhook: 503 при переполнении очереди


async def test_webhook_answers_503_when_queue_is_full_and_200_otherwise(session):
    cipher = TokenCipher(Fernet.generate_key().decode())
    row = await add_row(session, cipher, MASTER_ID, 1001)
    tg = FakeTelegram()
    factory = make_session_factory(TEST_DATABASE_URL)
    registry = BotRegistry(factory, cipher, http_session_factory=lambda: tg)
    await registry.load_all()
    dp = build_dispatcher(factory, workflow_data={"bot_registry": registry})
    release = asyncio.Event()

    async def slow_feed(bot, update):
        await release.wait()

    dp.feed_update = slow_feed
    admission = UpdateAdmission(max_total=1, max_per_bot=1, timeout_seconds=0.1)
    app = create_webhook_app(registry, dp, max_body_bytes=100_000, admission=admission)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    from app.multibot.webhook import SECRET_HEADER

    async def post(update_id):
        return await client.post(
            f"/tg/1001/{row.webhook_secret}",
            json=message_update(update_id, 5001, "/start"),
            headers={SECRET_HEADER: cipher.header_token(row.webhook_secret)},
        )

    try:
        first = asyncio.create_task(post(1))
        await asyncio.sleep(0.05)
        overflow = await post(2)  # место занято первым апдейтом и не освободилось за 0.1 с
        assert overflow.status_code == 503 and overflow.headers.get("Retry-After") == "5"
        release.set()
        assert (await first).status_code == 200
        assert (await post(3)).status_code == 200  # после освобождения — снова принимаем
    finally:
        release.set()
        await client.aclose()
        await registry.close()
        await factory.kw["bind"].dispose()
        for router in default_routers():
            router._parent_router = None
