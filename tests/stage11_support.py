"""Общие средства тестов изоляции (этап 11): фикстура «два полных мастера», снимки данных мастера, список таблиц.

Мастер A — №1 (его создаёт миграция), мастер B — №2. У обоих ОДИНАКОВЫЙ набор данных (все сущности из ТЗ), но значения
намеренно различаются (рейтинги, цены, рабочие часы, валюта, флаги клиента), поэтому смешение данных видно по самому
результату, а не только по «чужому id». Один и тот же Telegram-пользователь (SHARED_TG) — клиент обоих мастеров и
владелец записей у обоих.

Фикстура `two_masters` подключается в тестовом модуле явным импортом:

    from tests.stage11_support import two_masters  # noqa: F401  (фикстура pytest)
"""

import datetime as dt
from types import SimpleNamespace

import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.database.models  # noqa: F401  (регистрирует все таблицы в Base.metadata)
from app.database.models import (
    AdminReminder,
    BlockedPeriod,
    Base,
    Booking,
    BookingLimitRequest,
    BookingStatus,
    BreakPeriod,
    ClientAccessRequest,
    Master,
    MasterBot,
    MasterClient,
    MasterDeferredMessage,
    MasterProfile,
    MasterQuietMode,
    MasterSettings,
    Notification,
    NotificationStatus,
    NotificationType,
    PortfolioItem,
    Recurrence,
    Review,
    Service,
    SuperadminAction,
    User,
    WorkingDayOverride,
    WorkingHours,
)
from app.multibot.crypto import TokenCipher, generate_webhook_secret
from tests.conftest import RIGA_TZ, next_weekday, set_owner

A, B = 1, 2
MASTERS = (A, B)
TAG = {A: "A", B: "B"}
OWNER = {A: 9001, B: 9002}
SHARED_TG = 70001  # один человек — клиент обоих мастеров
ONLY_TG = {A: 70011, B: 70012}  # клиент только своего мастера
BOT_ID = {A: 8100000001, B: 8200000002}  # длинные, как настоящие id ботов (маскирование токенов ждёт 5+ цифр)
# Фиктивные токены (формат Telegram, но не настоящие). Хвосты разные, чтобы утечку токена B в данные A было видно.
TOKEN = {A: f"{BOT_ID[A]}:{'Q' * 35}", B: f"{BOT_ID[B]}:{'W' * 35}"}
RATINGS = {A: (5, 4), B: (2, 1)}
PRICE = {A: 40, B: 70}
WORK_HOURS = {A: (dt.time(10), dt.time(19)), B: (dt.time(9), dt.time(18))}
BREAKS = {A: (dt.time(15), dt.time(16)), B: (dt.time(13), dt.time(14))}
OVERRIDE_HOURS = {A: (dt.time(11), dt.time(15)), B: (dt.time(12), dt.time(16))}  # особый день: у мастеров разные часы
CURRENCY = {A: "BYN", B: "EUR"}
MAX_ACTIVE = {A: 3, B: 5}
INVITE_TOKEN = {A: "inv-token-a", B: "inv-token-b"}

# Таблицы БЕЗ колонки master_id и обоснование, почему им она не нужна. Новая таблица без master_id и без записи здесь
# роняет tests/test_stage11_database.py: нужно явно решить, данные это мастера или нет.
NON_TENANT_TABLES: dict[str, str] = {
    "masters": "сам арендатор: идентификатор мастера — его собственный id",
    "users": "глобальная личность по telegram_id; отношения с мастером хранятся в master_clients (там master_id)",
    "reference_snapshots": "эталонная база владельца сервиса: колонка source_master_id намеренно без FK (эталон переживает мастера)",
    "reference_snapshot_photos": "файлы эталонной базы: принадлежат эталону (snapshot_id), а не мастеру",
    "superadmin_actions": "служебный журнал владельца сервиса, колонка target_master_id намеренно без FK (запись переживает мастера)",
}


def tenant_tables() -> list:
    """Таблицы с колонкой master_id — из metadata (новые таблицы попадают сюда сами)."""
    return sorted((t for t in Base.metadata.sorted_tables if "master_id" in t.c), key=lambda t: t.name)


async def snapshot(session: AsyncSession, master_id: int) -> dict[str, list[tuple]]:
    """Все строки всех таблиц данных мастера: {таблица: [строка, ...]} (включая updated_at — меняется при любой правке)."""
    result: dict[str, list[tuple]] = {}
    for table in tenant_tables():
        rows = (
            await session.execute(
                select(*table.c).where(table.c.master_id == master_id).order_by(*table.primary_key.columns)
            )
        ).all()
        result[table.name] = [tuple(row) for row in rows]
    return result


async def row_counts(session: AsyncSession, master_id: int) -> dict[str, int]:
    return {name: len(rows) for name, rows in (await snapshot(session, master_id)).items()}


def start_at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=RIGA_TZ)


async def _client(session: AsyncSession, user_id: int, master_id: int, **flags) -> None:
    session.add(MasterClient(master_id=master_id, user_id=user_id, **flags))
    await session.flush()


async def seed_two_masters(session: AsyncSession, cipher: TokenCipher) -> SimpleNamespace:
    """Создаёт мастеров A и B с одинаковым набором данных; возвращает пространство имён с обычными числами (id и т.п.)."""
    await set_owner(session, OWNER[A], A)
    session.add(Master(id=B, owner_telegram_id=OWNER[B]))
    await session.flush()

    day = next_weekday(0)  # понедельник через 2+ недели
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    ns = SimpleNamespace(
        cipher=cipher,
        day=day,
        now=now,
        fire_at=now - dt.timedelta(days=1),  # у обоих мастеров ОДИНАКОВОЕ время срабатывания напоминания (CAS-проверки)
        override_day=day + dt.timedelta(days=1),  # дата особого дня у обоих мастеров
    )
    for name in (
        "service_id",
        "inactive_service_id",
        "booking_id",
        "booking2_id",
        "manual_booking_id",
        "completed_booking_id",
        "review_ids",
        "portfolio_ids",
        "break_id",
        "block_id",
        "override_id",
        "notification_ids",
        "reminder_id",
        "access_request_id",
        "limit_request_id",
        "owner_id",
        "only_id",
        "settings_id",
        "quiet_id",
        "deferred_id",
        "profile_id",
        "bot_row_id",
        "webhook_secret",
    ):
        setattr(ns, name, {})

    # --- люди ---
    shared = User(telegram_id=SHARED_TG, username="shared_user", first_name="Shared")
    session.add(shared)
    await session.flush()
    ns.shared_id = shared.id
    for m in MASTERS:
        only = User(telegram_id=ONLY_TG[m], username=f"only_{TAG[m].lower()}", first_name=f"Only{TAG[m]}")
        owner = User(telegram_id=OWNER[m], username=f"owner_{TAG[m].lower()}", first_name=f"Owner{TAG[m]}")
        session.add_all([only, owner])
        await session.flush()
        ns.only_id[m], ns.owner_id[m] = only.id, owner.id

    for m in MASTERS:
        tag = TAG[m]
        # --- клиенты мастера и их флаги (у B общий клиент ограничен — у A нет) ---
        await _client(session, ns.shared_id, m, is_restricted=(m == B))
        await _client(session, ns.only_id[m], m)
        await _client(session, ns.owner_id[m], m, is_in_admin_panel=True)

        # --- профиль и настройки ---
        profile = MasterProfile(master_id=m, name=f"Мастер-{tag}", address=f"Адрес {tag}", timezone="Europe/Riga")
        settings_row = MasterSettings(master_id=m, currency=CURRENCY[m], max_active_bookings=MAX_ACTIVE[m])
        session.add_all([profile, settings_row])

        # --- услуги ---
        service = Service(master_id=m, name=f"Услуга-{tag}", price=PRICE[m], duration_minutes=60, active=True, sort_order=0)
        inactive = Service(master_id=m, name=f"Скрытая-{tag}", price=PRICE[m], duration_minutes=30, active=False, sort_order=1)
        session.add_all([service, inactive])
        await session.flush()
        ns.service_id[m], ns.inactive_service_id[m] = service.id, inactive.id

        # --- расписание ---
        for weekday in range(7):
            session.add(
                WorkingHours(master_id=m, weekday=weekday, is_working=True, start_time=WORK_HOURS[m][0], end_time=WORK_HOURS[m][1])
            )
        brk = BreakPeriod(master_id=m, weekday=0, start_time=BREAKS[m][0], end_time=BREAKS[m][1])
        block = BlockedPeriod(
            master_id=m,
            start_datetime=start_at(day, 17, 30),
            end_datetime=start_at(day, 18, 30),
            reason=f"блокировка-{tag}",
        )
        override = WorkingDayOverride(
            master_id=m, day=day + dt.timedelta(days=1), is_working=True,
            start_time=OVERRIDE_HOURS[m][0], end_time=OVERRIDE_HOURS[m][1],
        )
        session.add_all([brk, block, override])

        # --- записи: ВРЕМЯ одинаковое у обоих мастеров (допустимо), клиенты разные ---
        def booking(user_id, hour, **extra) -> Booking:
            return Booking(
                master_id=m,
                user_id=user_id,
                service_id=service.id,
                start_datetime=start_at(day, hour),
                end_datetime=start_at(day, hour + 1),
                status=extra.pop("status", BookingStatus.CONFIRMED.value),
                service_name_snapshot=service.name,
                service_price_snapshot=PRICE[m],
                service_duration_snapshot=60,
                **extra,
            )

        main_booking = booking(ns.shared_id, 12)
        second_booking = booking(ns.only_id[m], 14)
        manual_booking = booking(
            ns.owner_id[m], 16, manual_client_name=f"Мануал-{tag}", manual_client_contact=f"+000{m}", invite_token=INVITE_TOKEN[m]
        )
        past = start_at(day, 12) - dt.timedelta(days=60)
        done_booking = Booking(
            master_id=m,
            user_id=ns.shared_id,
            service_id=service.id,
            start_datetime=past,
            end_datetime=past + dt.timedelta(hours=1),
            status=BookingStatus.COMPLETED.value,
            service_name_snapshot=service.name,
            service_price_snapshot=PRICE[m],
            service_duration_snapshot=60,
        )
        session.add_all([main_booking, second_booking, manual_booking, done_booking])
        await session.flush()
        ns.booking_id[m], ns.booking2_id[m] = main_booking.id, second_booking.id
        ns.manual_booking_id[m], ns.completed_booking_id[m] = manual_booking.id, done_booking.id

        # --- уведомления по записи: одно «созревшее», одно будущее ---
        due = Notification(
            master_id=m,
            booking_id=main_booking.id,
            type=NotificationType.REMINDER_24H.value,
            scheduled_at=now - dt.timedelta(hours=1),
            status=NotificationStatus.PENDING.value,
        )
        later = Notification(
            master_id=m,
            booking_id=main_booking.id,
            type=NotificationType.REMINDER_2H_CLIENT.value,
            scheduled_at=now + dt.timedelta(days=10),
            status=NotificationStatus.PENDING.value,
        )
        session.add_all([due, later])

        # --- отзывы и портфолио ---
        reviews = [
            Review(master_id=m, client_id=ns.shared_id, author_name=f"Ревью-{tag}-1", text=f"отзыв {tag} 1", rating=RATINGS[m][0]),
            Review(master_id=m, client_id=ns.only_id[m], author_name=f"Ревью-{tag}-2", text=f"отзыв {tag} 2", rating=RATINGS[m][1]),
        ]
        portfolio = [
            PortfolioItem(master_id=m, telegram_file_id=f"file-{tag}-{i}", title=f"Работа-{tag}-{i}", category=f"cat-{tag}", sort_order=i)
            for i in (1, 2)
        ]
        session.add_all(reviews + portfolio)

        # --- личное напоминание мастера, запросы клиентов, бот ---
        reminder = AdminReminder(
            master_id=m,
            text=f"напоминание {tag}",
            recurrence=Recurrence.ONCE.value,
            anchor_date=day,
            remind_time=dt.time(9),
            next_fire_at=ns.fire_at,
        )
        access = ClientAccessRequest(master_id=m, user_id=ns.shared_id, message=f"доступ {tag}")
        limit = BookingLimitRequest(master_id=m, user_id=ns.only_id[m], message=f"лимит {tag}")
        secret = generate_webhook_secret()
        bot_row = MasterBot(
            master_id=m,
            telegram_bot_id=BOT_ID[m],
            bot_username=f"bot{BOT_ID[m]}_bot",
            bot_token_encrypted=cipher.encrypt(TOKEN[m]),
            webhook_secret=secret,
            is_active=True,
        )
        # --- тихий режим (выключен, чтобы не влиять на остальные тесты; у мастеров разные окна) и отложенное им сообщение (у обоих уже просрочено) ---
        quiet = MasterQuietMode(
            master_id=m, enabled=False, start_time=dt.time(22) if m == A else dt.time(23), end_time=dt.time(8) if m == A else dt.time(9)
        )
        deferred = MasterDeferredMessage(
            master_id=m, text=f"отложено {tag}", reply_markup={"inline_keyboard": []}, expires_at=ns.fire_at
        )
        session.add_all([reminder, access, limit, bot_row, quiet, deferred])
        await session.flush()

        ns.review_ids[m] = [r.id for r in reviews]
        ns.portfolio_ids[m] = [p.id for p in portfolio]
        ns.break_id[m], ns.block_id[m], ns.override_id[m] = brk.id, block.id, override.id
        ns.notification_ids[m] = [due.id, later.id]
        ns.reminder_id[m], ns.access_request_id[m], ns.limit_request_id[m] = reminder.id, access.id, limit.id
        ns.settings_id[m], ns.profile_id[m] = settings_row.id, profile.id
        ns.quiet_id[m], ns.deferred_id[m] = quiet.id, deferred.id
        ns.bot_row_id[m], ns.webhook_secret[m] = bot_row.id, secret

    await session.commit()
    return ns


@pytest_asyncio.fixture
async def two_masters(session, monkeypatch):
    """Фикстура «два полных мастера» (см. docstring модуля).

    Режим запуска фиксируется как polling: стенд tests/tg.py определяет мастера по settings.master_id, а при RUN_MODE=webhook
    в локальном .env он отбрасывал бы апдейты (тесты стали бы «зелёными вхолостую»). Webhook-тесты работают через реестр ботов.
    """
    from app.config.settings import settings

    monkeypatch.setattr(settings, "run_mode", "polling")
    cipher = TokenCipher(Fernet.generate_key().decode())
    await session.execute(delete(SuperadminAction))
    await session.commit()
    return await seed_two_masters(session, cipher)
