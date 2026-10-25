"""Тестовые данные для нагрузки: мастера с активными ботами, услугами, графиком, клиентами и бронями.

    python -m tools.loadtest.seed --masters 100        # создать
    python -m tools.loadtest.seed --cleanup            # удалить только тестовые данные

ЗАЩИТА: адрес базы берётся ТОЛЬКО из LOADTEST_DATABASE_URL (DATABASE_URL не читается), имя базы обязано начинаться с «loadtest».
Тестовые данные помечены числами ≥ 900000000000 (владелец мастера, telegram_id клиентов); --cleanup удаляет только их.
Токены ботов фиктивные, хранятся зашифрованными ключом BOT_TOKEN_ENCRYPTION_KEY (как в рабочей системе) и нигде не печатаются.
"""

import argparse
import asyncio
import datetime as dt
import math
import os
import random
import secrets
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.database.models import (
    Booking,
    BreakPeriod,
    Master,
    MasterBot,
    MasterClient,
    MasterProfile,
    MasterSettings,
    Notification,
    Service,
    User,
    WorkingHours,
)
from app.multibot.crypto import ConfigurationError, TokenCipher, generate_webhook_secret
from tools.loadtest.common import BOT_ID_BASE, CLIENT_BASE, OWNER_BASE, TEST_OWNER_MIN, require_loadtest_db_url

TIMEZONE = "Europe/Minsk"
WORK_START = dt.time(9, 0)
WORK_END = dt.time(18, 0)
LUNCH = (dt.time(13, 0), dt.time(14, 0))
CELL_MINUTES = 30
CELLS_PER_DAY = 18  # 09:00–18:00 по 30 минут
LUNCH_CELLS = {8, 9}  # 13:00–14:00
MAX_MASTERS = 9000  # клиентский индекс влезает в 10_000 на мастера (см. CLIENT_BASE)

# (название, цена, длительность мин, свободное время после услуги мин)
SERVICES = [
    ("Маникюр классический", 35, 60, 10),
    ("Педикюр", 45, 90, 10),
    ("Гель-лак", 50, 90, 0),
    ("Коррекция", 25, 30, 0),
    ("Снятие покрытия", 15, 30, 0),
]
REMINDERS = (("reminder_24h", 24), ("reminder_2h_client", 2), ("reminder_2h_master", 2))


def _chunks(rows: list[dict], size: int = 2000):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


async def _bulk(conn: AsyncConnection, model, rows: list[dict]) -> None:
    for chunk in _chunks(rows):
        await conn.execute(insert(model.__table__), chunk)


def plan_bookings(rng: random.Random, days: list[dt.date], services: list[tuple], count: int) -> list[tuple[dt.date, int, int]]:
    """Брони без пересечений: [(день, индекс услуги, первая ячейка)]. Занимает «длительность + свободное время» целиком, обед не трогает."""
    busy: dict[dt.date, set[int]] = {day: set(LUNCH_CELLS) for day in days}
    placed: list[tuple[dt.date, int, int]] = []
    for _ in range(count * 40):
        if len(placed) >= count:
            break
        service_index = rng.randrange(len(services))
        _, _, duration, buffer = services[service_index]
        need = math.ceil((duration + buffer) / CELL_MINUTES)
        day = rng.choice(days)
        first = rng.randrange(0, CELLS_PER_DAY - need + 1)
        cells = set(range(first, first + need))
        if cells & busy[day]:
            continue
        busy[day] |= cells
        placed.append((day, service_index, first))
    return placed


async def _tables_ready(conn: AsyncConnection) -> bool:
    return (await conn.execute(text("SELECT to_regclass('public.masters') IS NOT NULL AND to_regclass('public.notifications') IS NOT NULL"))).scalar_one()


async def _test_master_ids(conn: AsyncConnection) -> list[int]:
    return list((await conn.execute(select(Master.id).where(Master.owner_telegram_id >= TEST_OWNER_MIN))).scalars())


async def cleanup(engine) -> None:
    async with engine.begin() as conn:
        if not await _tables_ready(conn):
            print("Таблиц нет (миграции не применены) — удалять нечего.")
            return
        ids = await _test_master_ids(conn)
        foreign = (await conn.execute(select(func.count()).select_from(Master).where(Master.owner_telegram_id < TEST_OWNER_MIN))).scalar_one()
        if ids:
            # Явный порядок: bookings.service_id — RESTRICT, поэтому сначала уведомления и записи, потом мастера (остальное — каскадом).
            await conn.execute(text("DELETE FROM notifications WHERE master_id = ANY(:ids)"), {"ids": ids})
            await conn.execute(text("DELETE FROM bookings WHERE master_id = ANY(:ids)"), {"ids": ids})
            await conn.execute(text("DELETE FROM masters WHERE id = ANY(:ids)"), {"ids": ids})
        users = await conn.execute(text("DELETE FROM users WHERE telegram_id >= :min"), {"min": TEST_OWNER_MIN})
    print(f"Удалено: тестовых мастеров — {len(ids)}, тестовых пользователей — {users.rowcount}. Не тестовых мастеров осталось нетронутыми: {foreign}.")


async def seed(engine, *, masters: int, clients: int, bookings: int, weeks: int, rng: random.Random) -> None:
    try:
        cipher = TokenCipher(os.environ.get("BOT_TOKEN_ENCRYPTION_KEY", ""))
    except ConfigurationError as exc:
        raise SystemExit(f"BOT_TOKEN_ENCRYPTION_KEY: {exc}") from None
    tz = ZoneInfo(TIMEZONE)
    now_utc = dt.datetime.now(dt.timezone.utc)
    today = dt.datetime.now(tz).date()
    days = [today + dt.timedelta(days=offset) for offset in range(1, weeks * 7 + 1) if (today + dt.timedelta(days=offset)).weekday() != 6]

    async with engine.begin() as conn:
        if not await _tables_ready(conn):
            raise SystemExit("В базе нет таблиц: сначала поднимите приложение (оно применяет миграции), см. README.")
        existing = await _test_master_ids(conn)
        if existing:
            raise SystemExit(f"В базе уже есть тестовые мастера ({len(existing)}). Сначала: python -m tools.loadtest.seed --cleanup")

        # 1) мастера
        await _bulk(conn, Master, [{"owner_telegram_id": OWNER_BASE + i, "is_active": True} for i in range(1, masters + 1)])
        master_ids = {
            owner - OWNER_BASE: master_id
            for master_id, owner in (await conn.execute(select(Master.id, Master.owner_telegram_id).where(Master.owner_telegram_id >= TEST_OWNER_MIN))).all()
        }

        # 2) профиль, настройки, бот, график
        bots, profiles, settings_rows, hours, breaks = [], [], [], [], []
        for i, master_id in master_ids.items():
            bot_id = BOT_ID_BASE + i
            token = f"{bot_id}:{secrets.token_urlsafe(26)[:35]}"  # формат реального токена; настоящим не является
            bots.append(
                {
                    "master_id": master_id,
                    "telegram_bot_id": bot_id,
                    "bot_username": f"loadtest{i}_bot",
                    "bot_token_encrypted": cipher.encrypt(token),
                    "webhook_secret": generate_webhook_secret(),
                    "is_active": True,
                }
            )
            profiles.append({"master_id": master_id, "name": f"Тестовый мастер {i}", "timezone": TIMEZONE, "address": f"Минск, ул. Тестовая, {i}"})
            settings_rows.append({"master_id": master_id})
            for weekday in range(7):
                working = weekday != 6
                hours.append(
                    {
                        "master_id": master_id,
                        "weekday": weekday,
                        "is_working": working,
                        "start_time": WORK_START if working else None,
                        "end_time": WORK_END if working else None,
                    }
                )
                if working:
                    breaks.append({"master_id": master_id, "weekday": weekday, "start_time": LUNCH[0], "end_time": LUNCH[1]})
        await _bulk(conn, MasterBot, bots)
        await _bulk(conn, MasterProfile, profiles)
        await _bulk(conn, MasterSettings, settings_rows)
        await _bulk(conn, WorkingHours, hours)
        await _bulk(conn, BreakPeriod, breaks)

        # 3) услуги
        service_rows = [
            {"master_id": master_id, "name": name, "price": price, "duration_minutes": duration, "buffer_minutes": buffer, "active": True, "sort_order": order}
            for master_id in master_ids.values()
            for order, (name, price, duration, buffer) in enumerate(SERVICES)
        ]
        await _bulk(conn, Service, service_rows)
        services_by_master: dict[int, list] = {}
        service_rows_db = (
            await conn.execute(select(Service.id, Service.master_id, Service.sort_order).where(Service.master_id.in_(list(master_ids.values()))))
        ).all()
        for service_id, master_id, order in service_rows_db:
            services_by_master.setdefault(master_id, []).append((order, service_id))

        # 4) клиенты (users глобальны по telegram_id) и связи «клиент мастера»
        user_rows, link_pairs = [], []
        for i in master_ids:
            for k in range(clients):
                user_rows.append({"telegram_id": CLIENT_BASE + i * 10_000 + k, "username": f"lt_{i}_{k}", "first_name": f"Клиент {i}-{k}", "last_name": None})
        await _bulk(conn, User, user_rows)
        user_id_by_tg = dict((await conn.execute(select(User.telegram_id, User.id).where(User.telegram_id >= TEST_OWNER_MIN))).all())
        for i, master_id in master_ids.items():
            for k in range(clients):
                link_pairs.append({"master_id": master_id, "user_id": user_id_by_tg[CLIENT_BASE + i * 10_000 + k]})
        await _bulk(conn, MasterClient, link_pairs)

        # 5) брони (по одной на клиента) и их напоминания — как делает приложение: прошедшие моменты не планируются
        booking_rows = []
        for i, master_id in master_ids.items():
            plan = plan_bookings(rng, days, SERVICES, min(bookings, clients))
            chosen_clients = rng.sample(range(clients), len(plan))
            service_ids = {order: sid for order, sid in services_by_master[master_id]}
            for (day, service_index, first_cell), client_k in zip(plan, chosen_clients):
                name, price, duration, buffer = SERVICES[service_index]
                start_local = dt.datetime.combine(day, WORK_START, tzinfo=tz) + dt.timedelta(minutes=CELL_MINUTES * first_cell)
                booking_rows.append(
                    {
                        "master_id": master_id,
                        "user_id": user_id_by_tg[CLIENT_BASE + i * 10_000 + client_k],
                        "service_id": service_ids[service_index],
                        "start_datetime": start_local,
                        "end_datetime": start_local + dt.timedelta(minutes=duration),
                        "status": "confirmed",
                        "service_name_snapshot": name,
                        "service_price_snapshot": price,
                        "service_duration_snapshot": duration,
                        "service_buffer_snapshot": buffer,
                    }
                )
        await _bulk(conn, Booking, booking_rows)

        notification_rows = []
        for booking_id, master_id, start in (
            await conn.execute(select(Booking.id, Booking.master_id, Booking.start_datetime).where(Booking.master_id.in_(list(master_ids.values()))))
        ).all():
            for notification_type, hours_before in REMINDERS:
                scheduled_at = start - dt.timedelta(hours=hours_before)
                if scheduled_at > now_utc:
                    notification_rows.append(
                        {"master_id": master_id, "booking_id": booking_id, "type": notification_type, "scheduled_at": scheduled_at, "status": "pending"}
                    )
        await _bulk(conn, Notification, notification_rows)

    print(
        f"Создано: мастеров — {masters}, клиентов — {len(user_rows)}, броней — {len(booking_rows)}, "
        f"напоминаний в очереди — {len(notification_rows)}. Брони — на ближайшие {weeks} нед., без пересечений."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--masters", type=int, default=100, help="сколько мастеров создать (по умолчанию 100)")
    parser.add_argument("--clients", type=int, default=200, help="клиентов на мастера (по умолчанию 200)")
    parser.add_argument("--bookings", type=int, default=60, help="броней на мастера (по умолчанию 60; не больше числа клиентов)")
    parser.add_argument("--weeks", type=int, default=3, help="на сколько недель вперёд брони (по умолчанию 3)")
    parser.add_argument("--random-seed", type=int, default=12, help="зерно генератора: одинаковые данные при повторе")
    parser.add_argument("--cleanup", action="store_true", help="удалить ТОЛЬКО тестовые данные (владелец/клиенты с id ≥ 900000000000) и выйти")
    args = parser.parse_args(argv)
    if not args.cleanup and not 1 <= args.masters <= MAX_MASTERS:
        parser.error(f"--masters: от 1 до {MAX_MASTERS}")
    if not 1 <= args.clients <= 9999 or args.bookings < 0 or args.weeks < 1:
        parser.error("--clients: 1–9999, --bookings ≥ 0, --weeks ≥ 1")
    return args


async def amain(args: argparse.Namespace) -> None:
    url = require_loadtest_db_url()  # проверка имени базы — ДО любого подключения
    engine = create_async_engine(url)
    try:
        if args.cleanup:
            await cleanup(engine)
        else:
            await seed(engine, masters=args.masters, clients=args.clients, bookings=args.bookings, weeks=args.weeks, rng=random.Random(args.random_seed))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    asyncio.run(amain(parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
