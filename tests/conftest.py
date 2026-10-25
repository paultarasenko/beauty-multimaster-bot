import os

# Этап 12C: пул БД по умолчанию стал больше (10 + 15). Тесты создают много движков и не все закрывают, поэтому в тестах
# пул небольшой — иначе набор упирается в max_connections тестовой PostgreSQL. Задаётся ДО импорта app.* (движок строится при импорте).
os.environ.setdefault("DB_POOL_SIZE", "3")
os.environ.setdefault("DB_MAX_OVERFLOW", "5")
import asyncio
import datetime as dt
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Master, MasterClient, MasterProfile, Service, User
from app.database.session import make_session_factory
from tests import fsm_utils

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5434/beauty_bot_test"
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MASTER_ID = 1  # мастер №1 создаёт миграция
RIGA_TZ = dt.timezone(dt.timedelta(hours=2))  # фиксированный offset — достаточно для тестов бизнес-логики


def next_weekday(weekday: int, min_days_ahead: int = 14) -> dt.date:
    """Ближайший день недели `weekday` (0 — понедельник) не раньше чем через `min_days_ahead` дней.

    Тесты с БД создают записи через настоящие часы (`BookingService` сверяется с `datetime.now`),
    поэтому зашитая дата рано или поздно оказывается в прошлом, и тесты краснеют без всякого изменения кода.
    """
    target = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=min_days_ahead)
    return target + dt.timedelta(days=(weekday - target.weekday()) % 7)


@pytest.fixture(autouse=True)
def _pin_run_mode_polling(monkeypatch):
    """Тесты не зависят от RUN_MODE из локального .env.

    Стенд tests/tg.py определяет мастера по settings.master_id (polling); при RUN_MODE=webhook в .env resolve_tenant
    отбрасывает апдейты с TenantNotAvailableError («Режим webhook без реестра ботов»). Webhook-тесты сами ставят
    run_mode через monkeypatch.setattr и работают через реестр ботов.
    """
    from app.config.settings import settings

    monkeypatch.setattr(settings, "run_mode", "polling")


TABLES_TO_CLEAN = [
    "master_bots",
    "admin_reminders",
    "notifications",
    "bookings",
    "blocked_periods",
    "breaks",
    "working_hours",
    "working_day_overrides",
    "services",
    "master_clients",
    "reviews",
    "portfolio_items",
    "users",
    "master_profile",
    "master_settings",
    "master_quiet_mode",
    "master_deferred_messages",
    "reference_snapshots",
]


def _check_test_database_is_safe() -> None:
    """Фикстуры делают TRUNCATE — запрещаем запуск против любой БД, имя которой не оканчивается на _test."""
    db_name = make_url(TEST_DATABASE_URL).database or ""
    if not db_name.endswith("_test"):
        pytest.exit(
            f"TEST_DATABASE_URL указывает на БД '{db_name}': тесты очищают таблицы, "
            "имя тестовой БД должно оканчиваться на '_test'.",
            returncode=2,
        )


async def _can_connect(dsn: str) -> None:
    conn = await asyncpg.connect(dsn, timeout=5)
    await conn.close()


@pytest.fixture(scope="session")
def _test_database() -> None:
    """Один раз за прогон (подключается фикстурой `session`, чистым unit-тестам БД не нужна): проверяет доступность тестовой БД и приводит схему к `alembic upgrade head`.

    Без БД падаем сразу и понятно, а не получаем десятки ConnectionRefusedError с долгими таймаутами.
    """
    _check_test_database_is_safe()

    asyncpg_dsn = make_url(TEST_DATABASE_URL).set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        asyncio.run(_can_connect(asyncpg_dsn))
    except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
        pytest.exit(
            f"Тестовая БД недоступна ({exc!r}).\n"
            "Поднимите её: docker compose -f docker-compose.test.yml up -d --wait\n"
            "или задайте свою через переменную окружения TEST_DATABASE_URL.",
            returncode=2,
        )

    # alembic/env.py берёт URL из settings.database_url (читает DATABASE_URL) — подменяем только для подпроцесса.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.exit(f"alembic upgrade head не выполнился:\n{result.stderr or result.stdout}", returncode=2)

    # Миграция этапа 8 создаёт мастеру №1 профиль и настройки; тесты стартуют с чистого листа (профиль и настройки
    # тесты создают сами или работают со значениями по умолчанию), иначе первый тест с фикстурой master_profile упал бы
    # на уникальности master_id.
    async def _reset_stage8_rows() -> None:
        conn = await asyncpg.connect(asyncpg_dsn)
        try:
            await conn.execute("TRUNCATE TABLE master_settings, master_profile RESTART IDENTITY CASCADE")
        finally:
            await conn.close()

    asyncio.run(_reset_stage8_rows())


@pytest_asyncio.fixture
async def session(_test_database):
    session_factory = make_session_factory(TEST_DATABASE_URL)
    async with session_factory() as s:
        # Мастера №1 создаёт миграция, а его owner_telegram_id она берёт из ADMIN_TELEGRAM_ID окружения — приводим к
        # известному состоянию: владельца нет, пока тест не назначит его через set_owner().
        await s.execute(update(Master).where(Master.id == MASTER_ID).values(owner_telegram_id=None, is_active=True))
        await s.commit()
        yield s
        await s.rollback()
    # Чистим таблицы после каждого теста, чтобы тесты были независимы друг от друга.
    async with session_factory() as s:
        for table in TABLES_TO_CLEAN:
            await s.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))
        # Мастера №1 создаёт миграция — его не трогаем. Остальные (создаются тестами изоляции) удаляем;
        # их данные уже очищены выше.
        await s.execute(text("DELETE FROM masters WHERE id <> 1"))
        await s.execute(update(Master).where(Master.id == MASTER_ID).values(owner_telegram_id=None, is_active=True))
        await s.commit()


@pytest_asyncio.fixture
async def master_profile(session: AsyncSession) -> MasterProfile:
    mp = MasterProfile(master_id=MASTER_ID, name="Test Master", timezone="Europe/Riga")
    session.add(mp)
    await session.flush()
    await session.commit()
    return mp


@pytest_asyncio.fixture
async def test_user(session: AsyncSession) -> User:
    user = User(telegram_id=111111, username="client1", first_name="Ivan")
    session.add(user)
    await session.flush()
    session.add(MasterClient(master_id=MASTER_ID, user_id=user.id))
    await session.commit()
    return user


@pytest_asyncio.fixture
async def test_user_2(session: AsyncSession) -> User:
    user = User(telegram_id=222222, username="client2", first_name="Maria")
    session.add(user)
    await session.flush()
    session.add(MasterClient(master_id=MASTER_ID, user_id=user.id))
    await session.commit()
    return user


@pytest_asyncio.fixture
async def manicure_service(session: AsyncSession) -> Service:
    service = Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)
    await session.flush()
    await session.commit()
    return service


@pytest_asyncio.fixture
async def long_service(session: AsyncSession) -> Service:
    """Услуга на 90 минут — для проверки edge cases с длинными процедурами."""
    service = Service(master_id=MASTER_ID, name="Наращивание", price=60, duration_minutes=90, active=True, sort_order=1)
    session.add(service)
    await session.flush()
    await session.commit()
    return service


async def set_owner(session: AsyncSession, owner_telegram_id: int | None, master_id: int = MASTER_ID) -> None:
    """Назначает владельца мастера (masters.owner_telegram_id) и коммитит. None — мастер без владельца."""
    await session.execute(update(Master).where(Master.id == master_id).values(owner_telegram_id=owner_telegram_id))
    await session.commit()


async def make_client(session: AsyncSession, telegram_id: int, master_id: int = MASTER_ID, **user_fields) -> User:
    """Пользователь (или уже существующий по telegram_id) + связь «клиент мастера» с этим мастером."""
    from sqlalchemy import select

    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, **user_fields)
        session.add(user)
        await session.flush()
    link = (
        await session.execute(
            select(MasterClient).where(MasterClient.master_id == master_id, MasterClient.user_id == user.id)
        )
    ).scalar_one_or_none()
    if link is None:
        session.add(MasterClient(master_id=master_id, user_id=user.id))
        await session.flush()
    return user


async def client_row(session: AsyncSession, user_or_id, master_id: int = MASTER_ID) -> MasterClient:
    """Свежая строка master_clients (флаги ограничений клиента у этого мастера)."""
    from sqlalchemy import select

    user_id = user_or_id if isinstance(user_or_id, int) else user_or_id.id
    result = await session.execute(
        select(MasterClient)
        .where(MasterClient.master_id == master_id, MasterClient.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def set_client(session: AsyncSession, user_or_id, master_id: int = MASTER_ID, **flags) -> MasterClient:
    """Выставляет флаги клиента у мастера (is_restricted, is_blocked_forever, over_limit_cancels, ...) и коммитит."""
    client = await client_row(session, user_or_id, master_id)
    for name, value in flags.items():
        setattr(client, name, value)
    await session.commit()
    return client


# ---------- Redis / хранилище FSM (этап 7) ----------

# Модули, чьи сценарии с FSM прогоняются на обоих хранилищах (memory и настоящий Redis). Параметризуются только тесты,
# которые реально используют FSM-стенд (tg_harness / webhook-фикстуры), чистые юнит-тесты внутри этих модулей — один раз.
FSM_PARAMETRIZED_MODULES = {
    "test_stage5_e2e",
    "test_stage5_callbacks",
    "test_client_flows",
    "test_book_from_schedule",
    "test_manual_booking_invites",
    "test_booking_ownership",
    "test_schedule_screen",
    "test_bookings_menu",
    "test_service_input",
    "test_access_requests",
    "test_limit_requests",
    "test_admin_reminder_handlers",
    "test_handlers_wiring",
    "test_owner_auth",
}
_FSM_FIXTURES = {"bots", "harness"}


def pytest_generate_tests(metafunc):
    if metafunc.module.__name__.rsplit(".", 1)[-1] not in FSM_PARAMETRIZED_MODULES:
        return
    try:
        uses_fsm = "tg_harness" in metafunc.function.__code__.co_names or "tg_harness" in __import__("inspect").getsource(metafunc.function)
    except (OSError, TypeError):
        uses_fsm = False
    if uses_fsm or _FSM_FIXTURES & set(metafunc.fixturenames):
        metafunc.parametrize("fsm_kind", ["memory", "redis"], indirect=True)


@pytest.fixture(scope="session")
def redis_available() -> bool:
    return fsm_utils.redis_is_available()


@pytest.fixture
def redis_url(redis_available) -> str:
    """URL настоящего Redis для тестов. Если Redis недоступен — тест пропускается с понятной причиной."""
    if not redis_available:
        pytest.skip(fsm_utils.SKIP_REASON)
    return fsm_utils.TEST_REDIS_URL


@pytest.fixture
def fsm_prefix(redis_url):
    """Уникальный префикс ключей FSM для теста; ключи с ним удаляются после теста."""
    prefix = fsm_utils.new_prefix()
    yield prefix
    fsm_utils.purge_prefix(prefix)


@pytest.fixture(autouse=True)
def fsm_kind(request):
    """Какое хранилище FSM используют стенды в этом тесте: memory (по умолчанию) или redis (параметризация)."""
    kind = getattr(request, "param", "memory")
    if kind == "redis":
        request.getfixturevalue("redis_url")  # skip, если Redis недоступен
        prefix = request.getfixturevalue("fsm_prefix")
    else:
        prefix = "bbot:test"
    fsm_utils.ActiveFsm.kind, fsm_utils.ActiveFsm.prefix = kind, prefix
    yield kind
    fsm_utils.ActiveFsm.kind, fsm_utils.ActiveFsm.prefix = "memory", "bbot:test"
