"""Подделанные callback_data и «залежавшиеся» FSM-данные с id ЧУЖОГО мастера (этап 5).

Бот мастера A получает нажатия с id объектов мастера B. Для каждой группы хендлеров проверяем три вещи:
1) данные в БД не изменились (снимок всех таблиц данных мастеров до и после);
2) пользователь получил понятный ответ, а не «Что-то пошло не так» (стектрейса нет);
3) ответ на чужой id ТОЧНО такой же, как на несуществующий: существование чужих записей не раскрывается.
"""

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import pytest_asyncio
from aiogram.fsm.state import State
from sqlalchemy import text

from app.bot.handlers.admin import access_requests, add_booking, block_time, bookings as admin_bookings
from app.bot.handlers.admin import client_access, limit_requests, portfolio, reminders, reviews, services
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import content as client_content
from app.bot.handlers.client import my_booking, reminder_actions
from app.bot.keyboards.callback_data import (
    AdminAccessRequestCallback,
    AdminBlockCallback,
    AdminBookingCallback,
    AdminBufferCallback,
    AdminClientAccessCallback,
    AdminDurationCallback,
    AdminLimitCommentCallback,
    AdminLimitRequestCallback,
    AdminPortfolioCallback,
    AdminReminderCallback,
    AdminReviewDeleteCallback,
    AdminServiceCallback,
    BookingActionCallback,
    DateCallback,
    PortfolioCallback,
    ReminderActionCallback,
    ServiceCallback,
    TimeCallback,
)
from app.bot.middlewares.error_handling import GENERIC_ERROR_TEXT
from app.bot.states.booking_states import (
    AdminAddBookingFSM,
    AdminPortfolioFSM,
    AdminServiceFSM,
    BookingFSM,
    MasterReplyFSM,
    RescheduleFSM,
)
from app.database.models import (
    AdminReminder,
    BlockedPeriod,
    BookingLimitRequest,
    ClientAccessRequest,
    Master,
    MasterProfile,
    PortfolioItem,
    Recurrence,
    Review,
    Service,
    WorkingHours,
)
from app.services.booking_service import BookingService
from tests.conftest import RIGA_TZ, make_client, next_weekday, set_owner
from tests.tg import tg_harness

A, B = 1, 2
ADMIN_A, OWNER_B = 999, 777
BOTH_TG = 333333  # клиент ОБОИХ мастеров; его запись у B для бота A — чужая, хотя «его» по telegram_id
A_ONLY_TG = 111111
B_ONLY_TG = 222222
MISSING = 999_999

TABLES = (
    "services", "bookings", "notifications", "reviews", "portfolio_items", "blocked_periods", "admin_reminders",
    "master_clients", "booking_limit_requests", "client_access_requests", "working_hours", "master_profile",
)


async def snapshot(session) -> dict[str, list]:
    session.expire_all()
    result = {}
    for table in TABLES:
        rows = (await session.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all()
        result[table] = [tuple(row) for row in rows]
    return result


@pytest_asyncio.fixture
async def two(session):
    """Два мастера со своими данными в каждой таблице. A создаётся первым, поэтому id объектов B не совпадают с id A."""
    await set_owner(session, ADMIN_A)
    session.add(Master(id=B, owner_telegram_id=OWNER_B))
    await session.flush()
    day = next_weekday(0)
    start = dt.datetime.combine(day, dt.time(12), tzinfo=RIGA_TZ)
    ids: dict[str, int] = {}
    objs: dict[str, Any] = {}
    for master in (A, B):
        session.add(MasterProfile(master_id=master, name=f"Мастер {master}", timezone="Europe/Riga"))
        for weekday in range(7):
            session.add(WorkingHours(master_id=master, weekday=weekday, is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
        service = Service(master_id=master, name=f"Услуга {master}", price=40, duration_minutes=60, active=True)
        session.add(service)
        await session.flush()
        objs[f"service_{master}"] = service
    a_only = await make_client(session, A_ONLY_TG, A, first_name="OnlyA", username="only_a")
    b_only = await make_client(session, B_ONLY_TG, B, first_name="OnlyB", username="only_b")
    both = await make_client(session, BOTH_TG, A, first_name="Both", username="both")
    await make_client(session, BOTH_TG, B)
    await session.flush()
    for master, user in ((A, a_only), (B, both)):
        booking = await BookingService(session, RIGA_TZ, master_id=master).create_booking(user.id, objs[f"service_{master}"].id, start)
        objs[f"booking_{master}"] = booking
        review = Review(master_id=master, client_id=user.id, author_name="x", text="отзыв", rating=5)
        item = PortfolioItem(master_id=master, telegram_file_id=f"f{master}", title="t")
        block = BlockedPeriod(master_id=master, start_datetime=start + dt.timedelta(hours=4), end_datetime=start + dt.timedelta(hours=5))
        reminder = AdminReminder(
            master_id=master, text="напоминание", recurrence=Recurrence.ONCE.value, anchor_date=day,
            remind_time=dt.time(9), next_fire_at=start - dt.timedelta(days=1),
        )
        limit = BookingLimitRequest(master_id=master, user_id=user.id, message="хочу ещё")
        access = ClientAccessRequest(master_id=master, user_id=user.id, message="пустите")
        session.add_all([review, item, block, reminder, limit, access])
        for name, obj in (("review", review), ("portfolio", item), ("block", block), ("reminder", reminder), ("limit", limit), ("access", access)):
            objs[f"{name}_{master}"] = obj
    await session.commit()
    ids = {
        "service": objs["service_2"].id, "booking": objs["booking_2"].id, "review": objs["review_2"].id,
        "portfolio": objs["portfolio_2"].id, "block": objs["block_2"].id, "reminder": objs["reminder_2"].id,
        "limit": objs["limit_2"].id, "access": objs["access_2"].id, "user": b_only.id,
    }
    return SimpleNamespace(ids=ids, day=day, start=start, a_service_id=objs["service_1"].id)


@dataclass
class Case:
    name: str
    routers: tuple
    user: int
    kind: str
    event: Callable[[int], tuple[str, Any]]  # id -> ("press", CallbackData|str) | ("text", str)
    state: State | None = None
    data: Callable[[int], dict] = field(default=lambda i: {})


def press(factory):
    return lambda i: ("press", factory(i))


def text_event(value):
    return lambda i: ("text", value)


def _cases() -> list[Case]:
    c: list[Case] = []
    adm = lambda name, routers, kind, factory, **kw: c.append(Case(name, routers, ADMIN_A, kind, press(factory), **kw))
    for action in ("delete", "confirm"):
        adm(f"admin_booking_{action}", (admin_bookings.router,), "booking", lambda i, a=action: AdminBookingCallback(booking_id=i, action=a))
    for action in ("open", "toggle", "buffer", "rename", "price", "description", "duration", "delete", "delete_yes"):
        adm(f"service_{action}", (services.router,), "service", lambda i, a=action: AdminServiceCallback(service_id=i, action=a))
    adm("service_buffer_pick", (services.router,), "service", lambda i: AdminBufferCallback(service_id=i, minutes=15))
    adm("service_duration_pick", (services.router,), "service", lambda i: AdminDurationCallback(service_id=i, minutes=30))
    for action in ("delete", "delete_confirm", "edit_title", "edit_description"):
        adm(f"portfolio_{action}", (portfolio.router,), "portfolio", lambda i, a=action: AdminPortfolioCallback(item_id=i, action=a))
    for action in ("open", "confirm"):
        adm(f"review_{action}", (reviews.router,), "review", lambda i, a=action: AdminReviewDeleteCallback(review_id=i, action=a))
    for action in ("unblock", "confirm"):
        adm(f"block_{action}", (block_time.router,), "block", lambda i, a=action: AdminBlockCallback(block_id=i, action=a))
    for action in ("open", "delete", "confirm"):
        adm(f"reminder_{action}", (reminders.router,), "reminder", lambda i, a=action: AdminReminderCallback(action=a, recurrence="none", reminder_id=i))
    for action in ("restrict", "allow"):
        adm(f"client_{action}", (client_access.router,), "user", lambda i, a=action: AdminClientAccessCallback(user_id=i, action=a))
    for action in ("approve", "reject"):
        adm(f"limit_decision_{action}", (limit_requests.router,), "limit", lambda i, a=action: AdminLimitRequestCallback(request_id=i, action=a))
    for action in ("yes", "no"):
        adm(f"limit_comment_{action}", (limit_requests.router,), "limit", lambda i, a=action: AdminLimitCommentCallback(request_id=i, action=a))
    for action in ("open", "block"):
        adm(f"access_{action}", (access_requests.router,), "access", lambda i, a=action: AdminAccessRequestCallback(request_id=i, action=a))

    # --- клиент (человек — клиент обоих мастеров, чужая запись — его собственная у мастера B) ---
    cli = lambda name, routers, kind, factory, **kw: c.append(Case(name, routers, BOTH_TG, kind, press(factory), **kw))
    for action in ("cancel", "cancel_back", "cancel_confirm", "reschedule"):
        cli(f"my_booking_{action}", (my_booking.router,), "booking", lambda i, a=action: BookingActionCallback(booking_id=i, action=a))
    for action in ("still_valid", "cancel", "reschedule"):
        cli(f"reminder_{action}_client", (reminder_actions.router,), "booking", lambda i, a=action: ReminderActionCallback(booking_id=i, action=a))
    cli("portfolio_view", (client_content.router,), "portfolio", lambda i: PortfolioCallback(item_id=i))
    cli("booking_select_service", (client_booking.router,), "service", lambda i: ServiceCallback(service_id=i), state=BookingFSM.select_service)

    # --- FSM: id чужого мастера «залежался» в данных состояния ---
    iso = lambda: next_weekday(0).isoformat()
    for state, name, value in (
        (AdminServiceFSM.edit_name, "name", "Взлом"), (AdminServiceFSM.edit_price, "price", "1"),
        (AdminServiceFSM.edit_duration, "duration", "45"), (AdminServiceFSM.edit_description, "description", "взлом"),
    ):
        c.append(Case(f"fsm_service_{name}", (services.router,), ADMIN_A, "service", text_event(value), state, lambda i: {"service_id": i}))
    c.append(Case("fsm_portfolio_title", (portfolio.router,), ADMIN_A, "portfolio", text_event("Взлом"), AdminPortfolioFSM.edit_title, lambda i: {"item_id": i}))
    c.append(Case("fsm_master_reply", (limit_requests.router,), ADMIN_A, "limit", text_event("привет"), MasterReplyFSM.waiting_text, lambda i: {"request_id": i}))
    c.append(Case("fsm_admin_add_booking_date", (add_booking.router,), ADMIN_A, "service", press(lambda i: DateCallback(date=iso())), AdminAddBookingFSM.select_date, lambda i: {"service_id": i}))
    c.append(Case("fsm_booking_date", (client_booking.router,), BOTH_TG, "service", press(lambda i: DateCallback(date=iso())), BookingFSM.select_date, lambda i: {"service_id": i}))
    c.append(Case(
        "fsm_reschedule_time", (my_booking.router,), BOTH_TG, "booking",
        press(lambda i: TimeCallback(ts=int(dt.datetime.combine(next_weekday(0), dt.time(15), tzinfo=RIGA_TZ).timestamp()))),
        RescheduleFSM.select_time, lambda i: {"booking_id": i, "service_duration": 60},
    ))
    return c


CASES = _cases()


async def _run(case: Case, object_id: int):
    async with tg_harness(*case.routers, error_middleware=True, master_id=A) as h:
        if case.state is not None:
            await h.set_state(case.user, case.state, **case.data(object_id))
        kind, payload = case.event(object_id)
        if kind == "press":
            await h.press(case.user, payload)
        else:
            await h.send_text(case.user, payload)
        return h.tg.everything_shown(), h.tg.alerts(), await h.get_state(case.user), [type(m).__name__ for m in h.tg.requests if type(m).__name__ == "SendMessage" and m.chat_id != case.user]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_foreign_id_changes_nothing_and_looks_like_missing_id(session, two, case):
    before = await snapshot(session)
    foreign = await _run(case, two.ids[case.kind])
    assert await snapshot(session) == before, "подделанный id чужого мастера изменил данные"
    missing = await _run(case, MISSING)
    assert await snapshot(session) == before

    shown, _, _, to_others = foreign
    assert shown, "пользователь должен получить понятный ответ, а не тишину"
    assert GENERIC_ERROR_TEXT not in shown, "хендлер упал на подделанном id (общая ошибка вместо «не найдено»)"
    assert to_others == [], "чужому человеку (мастеру/клиенту) ничего отправляться не должно"
    assert foreign == missing, "ответ на чужой id отличается от ответа на несуществующий — это раскрывает существование записей"


def test_case_matrix_covers_every_group():
    names = " ".join(c.name for c in CASES)
    for group in ("admin_booking", "service_", "portfolio_", "review_", "block_", "reminder_", "client_", "limit_", "access_", "my_booking", "fsm_"):
        assert group in names
    assert len(CASES) >= 50


# ---------- свои объекты при этом работают (тест не «зелёный» из-за того, что всё всегда отказывает) ----------


async def test_own_objects_still_work_for_the_same_handlers(session, two):
    async with tg_harness(services.router, reviews.router, error_middleware=True, master_id=A) as h:
        a_service = two.a_service_id
        await h.press(ADMIN_A, AdminServiceCallback(service_id=a_service, action="toggle"))
        assert GENERIC_ERROR_TEXT not in h.tg.everything_shown()
    session.expire_all()
    own = await session.get(Service, a_service)
    assert own.active is False  # свою услугу мастер A выключил, услуга B не тронута
    assert (await session.get(Service, two.ids["service"])).active is True


# ---------- не-владелец не проходит ни один админский хендлер ----------


async def test_stranger_gets_nothing_from_any_admin_callback_or_state(session, two):
    """Посторонний (и даже мастер B в боте A) шлёт каждый админский callback и текст в каждом админском состоянии."""
    from tests.test_stage5_guards import _admin_routers

    routers = [r for name, r in _admin_routers() if name != "content"]  # content — общий с клиентами
    admin_cases = [c for c in CASES if c.user == ADMIN_A]
    before = await snapshot(session)
    for stranger in (555, OWNER_B):
        for case in admin_cases:
            async with tg_harness(*routers, master_id=A) as h:
                if case.state is not None:
                    await h.set_state(stranger, case.state, **case.data(1))
                kind, payload = case.event(1)
                await (h.press(stranger, payload) if kind == "press" else h.send_text(stranger, payload))
                assert h.tg.requests == [], f"{case.name}: не-владелец получил реакцию админского хендлера"
                if case.state is not None:
                    assert await h.get_state(stranger) == case.state.state
    assert await snapshot(session) == before
