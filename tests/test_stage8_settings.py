"""Этап 8: индивидуальные настройки мастера — БД, сервисы, планировщик, интерфейс мастера и клиента, изоляция мастеров.

Telegram замокан (tests/tg.py), БД настоящая.
"""

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.bot.handlers.admin import master_settings as admin_settings
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import content as client_content
from app.bot.handlers.client import my_booking as client_my_booking
from app.bot.handlers.client import reminder_actions
from app.bot.keyboards.admin_keyboards import BTN_SETTINGS
from app.bot.keyboards.callback_data import (
    AdminSettingsCallback,
    BookingActionCallback,
    ConfirmCallback,
    ReminderActionCallback,
    ReviewsCallback,
)
from app.bot.states.booking_states import AdminSettingsFSM, BookingFSM, RescheduleFSM
from app.database.models import (
    Booking,
    Master,
    MasterProfile,
    MasterSettings,
    Notification,
    NotificationStatus,
    NotificationType,
    Service,
    User,
    WorkingHours,
)
from app.database.repositories.master_settings_repository import MasterSettingsRepository
from app.database.session import make_session_factory
from app.multibot import management
from app.services.booking_service import BookingService, booking_service_for
from app.services.exceptions import BookingLimitReachedError, SlotNotAvailableError
from app.services.master_settings_service import ensure_master_defaults, load_master_settings
from app.services.reminder_service import ReminderService
from app.tenancy import TenantContext
from app.utils.master_settings import MasterSettingsData
from tests.conftest import MASTER_ID, RIGA_TZ, TEST_DATABASE_URL, make_client, next_weekday, set_owner
from tests.test_multibot_registry import add_master
from tests.tg import tg_harness

ADMIN = 999
CLIENT = 111111  # test_user
OTHER_MASTER = 2
OTHER_OWNER = 888
DAY = next_weekday(4)


def at(hour: int, minute: int = 0, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=RIGA_TZ)


async def settings_of(session, master_id: int = MASTER_ID) -> MasterSettingsData:
    session.expire_all()
    return await load_master_settings(session, master_id)


async def update_settings(session, master_id: int = MASTER_ID, **values) -> None:
    await MasterSettingsRepository(session, master_id).update_fields(**values)
    await session.commit()


async def tenant_for(session, master_id: int = MASTER_ID) -> TenantContext:
    master = await session.get(Master, master_id)
    return TenantContext(master_id=master_id, master=master, session=session)


async def add_working_day(session, master_id: int = MASTER_ID, day: dt.date = DAY) -> None:
    session.add(WorkingHours(master_id=master_id, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    await session.commit()


@pytest_asyncio.fixture
async def world(session, master_profile, test_user, manicure_service):
    await set_owner(session, ADMIN)
    await add_working_day(session)
    return {"service_id": manicure_service.id, "user_id": test_user.id}


# ---------- модель и репозиторий ----------


async def test_missing_row_means_defaults_and_nothing_is_written(session):
    assert await load_master_settings(session, MASTER_ID) == MasterSettingsData()
    count = (await session.execute(select(func.count()).select_from(MasterSettings).where(MasterSettings.master_id == MASTER_ID))).scalar_one()
    assert count == 0


async def test_ensure_row_is_idempotent_and_has_defaults(session):
    repo = MasterSettingsRepository(session, MASTER_ID)
    first = await repo.ensure_row()
    second = await repo.ensure_row()
    await session.commit()
    assert first.id == second.id
    assert await repo.get_data() == MasterSettingsData()


async def test_update_and_reset_fields(session):
    repo = MasterSettingsRepository(session, MASTER_ID)
    await repo.update_fields(slot_interval_minutes=60, currency="RUB", cancellation_text="Отменено: {service}")
    await session.commit()
    data = await settings_of(session)
    assert (data.slot_interval_minutes, data.currency, data.cancellation_text) == (60, "RUB", "Отменено: {service}")
    await repo.reset_fields(["currency", "cancellation_text"])
    await session.commit()
    data = await settings_of(session)
    assert (data.slot_interval_minutes, data.currency, data.cancellation_text) == (60, "BYN", None)
    await repo.reset_fields()
    await session.commit()
    assert await settings_of(session) == MasterSettingsData()


async def test_unknown_field_is_refused(session):
    with pytest.raises(KeyError):
        await MasterSettingsRepository(session, MASTER_ID).update_fields(master_id=5)


@pytest.mark.parametrize(
    "values",
    [
        {"slot_interval_minutes": 20},
        {"max_active_bookings": 0},
        {"max_active_bookings": 11},
        {"cancel_streak_limit": 1},
        {"cancel_streak_limit": 11},
        {"currency": ""},
        {"currency": "TOOLONGCUR"},
        {"booking_confirmation_text": "x" * 1001},
        {"reminder_2h_text": "x" * 1001},
    ],
)
async def test_database_check_constraints_reject_bad_values(session, values):
    repo = MasterSettingsRepository(session, MASTER_ID)
    await repo.ensure_row()
    await session.commit()
    # IntegrityError (CHECK) или его родитель DBAPIError (слишком длинная строка отсекается самим типом VARCHAR(8))
    with pytest.raises(DBAPIError):
        await repo.update_fields(**values)
    await session.rollback()


async def test_one_settings_row_per_master(session):
    await MasterSettingsRepository(session, MASTER_ID).ensure_row()
    await session.commit()
    session.add(MasterSettings(master_id=MASTER_ID))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_settings_are_deleted_with_master(session):
    await add_master(session, OTHER_MASTER, OTHER_OWNER)
    await MasterSettingsRepository(session, OTHER_MASTER).ensure_row()
    await session.commit()
    await session.delete(await session.get(Master, OTHER_MASTER))
    await session.commit()
    assert (await session.execute(select(func.count()).select_from(MasterSettings).where(MasterSettings.master_id == OTHER_MASTER))).scalar_one() == 0


async def test_repository_requires_master_id():
    from app.database.repositories.base import MasterIdRequiredError

    with pytest.raises(MasterIdRequiredError):
        MasterSettingsRepository(object(), None)


# ---------- два мастера: настройки не смешиваются ----------


async def test_settings_of_two_masters_are_independent(session):
    await add_master(session, OTHER_MASTER, OTHER_OWNER)
    await update_settings(session, MASTER_ID, slot_interval_minutes=60, currency="RUB", allow_reviews=False)
    assert await settings_of(session, OTHER_MASTER) == MasterSettingsData()
    await update_settings(session, OTHER_MASTER, max_active_bookings=1, reminder_24h_enabled=False)
    mine = await settings_of(session, MASTER_ID)
    assert (mine.max_active_bookings, mine.reminder_24h_enabled) == (3, True)
    assert (mine.slot_interval_minutes, mine.currency, mine.allow_reviews) == (60, "RUB", False)


async def test_booking_services_of_two_masters_use_their_own_settings(session):
    await add_master(session, OTHER_MASTER, OTHER_OWNER)
    await update_settings(session, MASTER_ID, slot_interval_minutes=60, max_active_bookings=1)
    first = await booking_service_for(session, RIGA_TZ, await tenant_for(session, MASTER_ID))
    second = await booking_service_for(session, RIGA_TZ, await tenant_for(session, OTHER_MASTER))
    assert (first.availability.slot_interval_minutes, first.max_active_bookings) == (60, 1)
    assert (second.availability.slot_interval_minutes, second.max_active_bookings) == (30, 3)


async def test_tenant_context_caches_and_invalidates(session):
    tenant = await tenant_for(session)
    assert (await tenant.get_settings()).currency == "BYN"
    await update_settings(session, currency="USD")
    assert (await tenant.get_settings()).currency == "BYN"  # один снимок на апдейт
    tenant.invalidate_settings()
    assert (await tenant.get_settings()).currency == "USD"


async def test_tenant_context_without_session_refuses_clearly():
    tenant = TenantContext(master_id=1, master=Master(id=1))
    with pytest.raises(RuntimeError):
        await tenant.get_settings()


# ---------- новый мастер работает из коробки ----------


async def test_create_master_creates_profile_and_settings(session):
    factory = make_session_factory(TEST_DATABASE_URL)
    try:
        master_id = await management.create_master(factory, 4242)
    finally:
        await factory.kw["bind"].dispose()
    session.expire_all()
    profile = (await session.execute(select(MasterProfile).where(MasterProfile.master_id == master_id))).scalar_one()
    assert profile.timezone
    assert await settings_of(session, master_id) == MasterSettingsData()
    row_count = (await session.execute(select(func.count()).select_from(MasterSettings).where(MasterSettings.master_id == master_id))).scalar_one()
    assert row_count == 1


async def test_ensure_master_defaults_is_idempotent_and_keeps_existing(session, master_profile):
    await update_settings(session, currency="RUB")
    await ensure_master_defaults(session, MASTER_ID)
    await ensure_master_defaults(session, MASTER_ID)
    await session.commit()
    assert (await settings_of(session)).currency == "RUB"
    profiles = (await session.execute(select(func.count()).select_from(MasterProfile).where(MasterProfile.master_id == MASTER_ID))).scalar_one()
    assert profiles == 1


# ---------- BookingService ----------


async def test_slot_grid_follows_master_interval(session, world):
    default = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    assert len(await default.get_available_slots(DAY, 60)) == 17  # 10:00…18:00 с шагом 30 мин
    await update_settings(session, slot_interval_minutes=60)
    hourly = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    assert [s.hour for s in await hourly.get_available_slots(DAY, 60)] == list(range(10, 19))
    await update_settings(session, slot_interval_minutes=15)
    quarter = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    assert len(await quarter.get_available_slots(DAY, 60)) == 33


async def test_off_grid_time_cannot_be_booked_after_interval_change_but_old_booking_stays(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    old = await service.create_booking(world["user_id"], world["service_id"], at(14, 30))
    await session.commit()
    old_id = old.id  # id берём до rollback/expire_all: после них обращение к old лезло бы в БД вне greenlet
    await update_settings(session, slot_interval_minutes=60)
    hourly = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    with pytest.raises(SlotNotAvailableError):
        await hourly.create_booking(world["user_id"], world["service_id"], at(11, 30))  # не по сетке 60 мин
    await session.rollback()
    session.expire_all()
    assert (await session.get(Booking, old_id)).start_datetime == at(14, 30)  # существующая запись не тронута
    slots = await hourly.get_available_slots(DAY, 60)
    assert at(14, 30) not in slots and at(14) not in slots  # занятое время не предлагается


async def test_booking_limit_follows_master_setting(session, world):
    await update_settings(session, max_active_bookings=1)
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    await service.create_booking(world["user_id"], world["service_id"], at(11), enforce_client_limits=True)
    await session.commit()
    with pytest.raises(BookingLimitReachedError):
        await service.create_booking(world["user_id"], world["service_id"], at(13), enforce_client_limits=True)
    await session.rollback()


async def test_default_limit_is_still_three(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    for hour in (10, 12, 14):
        await service.create_booking(world["user_id"], world["service_id"], at(hour), enforce_client_limits=True)
    await session.commit()
    with pytest.raises(BookingLimitReachedError):
        await service.create_booking(world["user_id"], world["service_id"], at(16), enforce_client_limits=True)
    await session.rollback()


async def test_raising_limit_allows_more_and_lowering_keeps_existing(session, world):
    await update_settings(session, max_active_bookings=5)
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    for hour in (10, 11, 12, 13):
        await service.create_booking(world["user_id"], world["service_id"], at(hour), enforce_client_limits=True)
    await session.commit()
    await update_settings(session, max_active_bookings=2)
    lowered = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    with pytest.raises(BookingLimitReachedError):
        await lowered.create_booking(world["user_id"], world["service_id"], at(16), enforce_client_limits=True)
    await session.rollback()
    count = (await session.execute(select(func.count()).select_from(Booking).where(Booking.master_id == MASTER_ID))).scalar_one()
    assert count == 4  # уже созданные записи остались


async def test_cancel_streak_limit_is_used_by_service(session, world):
    await update_settings(session, cancel_streak_limit=2)
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    assert service.cancel_streak_limit == 2


async def _notification_types(session) -> set[str]:
    session.expire_all()
    return set((await session.execute(select(Notification.type).where(Notification.master_id == MASTER_ID))).scalars())


async def test_all_three_reminders_are_created_by_default(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    assert await _notification_types(session) == {t.value for t in NotificationType}


@pytest.mark.parametrize(
    "flag,missing",
    [
        ("reminder_24h_enabled", NotificationType.REMINDER_24H),
        ("reminder_2h_enabled", NotificationType.REMINDER_2H_CLIENT),
        ("master_reminder_2h_enabled", NotificationType.REMINDER_2H_MASTER),
    ],
)
async def test_disabled_reminder_is_not_created_for_new_bookings(session, world, flag, missing):
    await update_settings(session, **{flag: False})
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    assert await _notification_types(session) == {t.value for t in NotificationType} - {missing.value}


async def test_all_reminders_off_creates_none(session, world):
    await update_settings(session, reminder_24h_enabled=False, reminder_2h_enabled=False, master_reminder_2h_enabled=False)
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    assert await _notification_types(session) == set()


# ---------- ReminderService: перепроверка в момент отправки ----------


async def _statuses(session) -> dict[str, str]:
    session.expire_all()
    rows = (await session.execute(select(Notification.type, Notification.status).where(Notification.master_id == MASTER_ID))).all()
    return dict(rows)


async def test_reminder_disabled_after_creation_is_cancelled_not_sent(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    booking = await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    await update_settings(session, reminder_24h_enabled=False)

    sent: list[int] = []

    async def send(b):
        sent.append(b.id)

    reminders = ReminderService(session, send, master_id=MASTER_ID)
    now = booking.start_datetime - dt.timedelta(hours=24)
    assert await reminders.process_due_notifications(now=now) == 0
    assert sent == []
    assert (await _statuses(session))[NotificationType.REMINDER_24H.value] == NotificationStatus.CANCELLED.value
    # остальные типы не затронуты: они ещё не созрели
    assert (await _statuses(session))[NotificationType.REMINDER_2H_CLIENT.value] == NotificationStatus.PENDING.value


async def test_enabled_reminder_is_sent(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    booking = await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    sent: list[int] = []

    async def send(b):
        sent.append(b.id)

    reminders = ReminderService(session, send, master_id=MASTER_ID)
    assert await reminders.process_due_notifications(now=booking.start_datetime - dt.timedelta(hours=24)) == 1
    assert sent == [booking.id]


async def test_each_master_reminders_follow_their_own_flag(session, world, test_user):
    await add_master(session, OTHER_MASTER, OTHER_OWNER)
    await add_working_day(session, OTHER_MASTER)
    other_service = Service(master_id=OTHER_MASTER, name="Стрижка", price=20, duration_minutes=60, active=True, sort_order=0)
    session.add(other_service)
    await session.flush()
    await make_client(session, CLIENT, OTHER_MASTER)
    await session.commit()
    await update_settings(session, OTHER_MASTER, reminder_24h_enabled=False)  # у мастера 2 выключено, у мастера 1 — нет

    bookings = {}
    for master_id, service_id in ((MASTER_ID, world["service_id"]), (OTHER_MASTER, other_service.id)):
        svc = await booking_service_for(session, RIGA_TZ, await tenant_for(session, master_id))
        bookings[master_id] = await svc.create_booking(world["user_id"], service_id, at(15))
        await session.commit()

    sent = {MASTER_ID: [], OTHER_MASTER: []}
    now = bookings[MASTER_ID].start_datetime - dt.timedelta(hours=24)
    for master_id in (MASTER_ID, OTHER_MASTER):
        async def send(b, master_id=master_id):
            sent[master_id].append(b.id)

        await ReminderService(session, send, master_id=master_id).process_due_notifications(now=now)
    assert sent[MASTER_ID] == [bookings[MASTER_ID].id]
    assert sent[OTHER_MASTER] == []


# ---------- интерфейс мастера ----------


async def _db_settings(session, master_id: int = MASTER_ID) -> MasterSettingsData:
    return await settings_of(session, master_id)


def cb(action: str, field: str = "none", value: str = "none") -> AdminSettingsCallback:
    return AdminSettingsCallback(action=action, field=field, value=value)


async def test_owner_opens_settings_screen_with_current_values(session):
    await set_owner(session, ADMIN)
    await update_settings(session, slot_interval_minutes=60, currency="RUB")
    async with tg_harness(admin_settings.router) as h:
        await h.send_text(ADMIN, BTN_SETTINGS)
        text = h.tg.sent_to(ADMIN)[0]
        assert "Интервал записи: 60 мин" in text and "Валюта: RUB" in text


async def test_stranger_cannot_open_or_change_settings(session):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.send_text(555, BTN_SETTINGS)
        await h.press(555, cb("set", "slot_interval_minutes", "60"))
        await h.press(555, cb("toggle", "allow_reviews"))
        assert h.tg.requests == []
    assert await _db_settings(session) == MasterSettingsData()


async def test_owner_of_another_master_cannot_change_this_masters_settings(session):
    await add_master(session, OTHER_MASTER, OTHER_OWNER)
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:  # бот мастера 1
        await h.press(OTHER_OWNER, cb("set", "slot_interval_minutes", "60"))
        assert h.tg.requests == []
    assert await _db_settings(session) == MasterSettingsData()
    assert await _db_settings(session, OTHER_MASTER) == MasterSettingsData()


@pytest.mark.parametrize("minutes", [15, 30, 60])
async def test_owner_sets_slot_interval(session, minutes):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("set", "slot_interval_minutes", str(minutes)))
    assert (await _db_settings(session)).slot_interval_minutes == minutes


@pytest.mark.parametrize(
    "field,value",
    [
        ("slot_interval_minutes", "20"),
        ("slot_interval_minutes", "abc"),
        ("slot_interval_minutes", "-15"),
        ("max_active_bookings", "0"),
        ("max_active_bookings", "11"),
        ("cancel_streak_limit", "1"),
        ("currency", "TOO LONG CUR"),
        ("master_id", "5"),
        ("reminder_24h_enabled", "0"),
        ("booking_confirmation_text", "x"),
    ],
)
async def test_forged_or_out_of_range_values_are_refused(session, field, value):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("set", field, value))
        assert h.tg.alerts(), "мастер должен увидеть, почему значение не принято"
    assert await _db_settings(session) == MasterSettingsData()


async def test_owner_changes_limits(session):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("set", "max_active_bookings", "5"))
        await h.press(ADMIN, cb("set", "cancel_streak_limit", "4"))
    data = await _db_settings(session)
    assert (data.max_active_bookings, data.cancel_streak_limit) == (5, 4)


@pytest.mark.parametrize("field", ["reminder_24h_enabled", "reminder_2h_enabled", "master_reminder_2h_enabled", "allow_reschedule", "allow_reviews"])
async def test_owner_toggles_switches_back_and_forth(session, field):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("toggle", field))
        assert getattr(await _db_settings(session), field) is False
        await h.press(ADMIN, cb("toggle", field))
        assert getattr(await _db_settings(session), field) is True


async def test_toggle_of_non_switch_is_refused(session):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("toggle", "currency"))
        await h.press(ADMIN, cb("toggle", "slot_interval_minutes"))
        assert len(h.tg.alerts()) == 2
    assert await _db_settings(session) == MasterSettingsData()


async def test_currency_by_button_and_by_text(session):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("set", "currency", "USD"))
        assert (await _db_settings(session)).currency == "USD"

        await h.press(ADMIN, cb("currency"))
        assert await h.get_state(ADMIN) == AdminSettingsFSM.enter_currency.state
        await h.send_text(ADMIN, "bad cur")  # пробел внутри
        assert await h.get_state(ADMIN) == AdminSettingsFSM.enter_currency.state
        assert (await _db_settings(session)).currency == "USD"
        await h.send_text(ADMIN, "₽")
        assert await h.get_state(ADMIN) is None
    assert (await _db_settings(session)).currency == "₽"


async def test_currency_input_is_cancelled_by_menu_button(session):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router, reset_input=True) as h:
        await h.set_state(ADMIN, AdminSettingsFSM.enter_currency)
        await h.send_text(ADMIN, "📅 Записи")  # кнопка меню прерывает ввод и не становится валютой
        assert await h.get_state(ADMIN) is None
    assert (await _db_settings(session)).currency == "BYN"


async def test_text_template_flow_validate_save_preview_reset(session):
    await set_owner(session, ADMIN)
    field = "booking_confirmation_text"
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("text", field))
        assert "стандартный" in h.tg.edited()[-1]

        await h.press(ADMIN, cb("text_edit", field))
        assert await h.get_state(ADMIN) == AdminSettingsFSM.enter_text.state

        await h.send_text(ADMIN, "Привет, {unknown}!")  # неизвестная подстановка
        assert await h.get_state(ADMIN) == AdminSettingsFSM.enter_text.state
        assert "{unknown}" in h.tg.sent_to(ADMIN)[-1] and "{service}" in h.tg.sent_to(ADMIN)[-1]
        assert (await _db_settings(session)).booking_confirmation_text is None

        await h.send_text(ADMIN, "x" * 1001)
        assert (await _db_settings(session)).booking_confirmation_text is None

        await h.send_text(ADMIN, "Ждём вас, {client_name}! {service}, {date} {time}")
        assert await h.get_state(ADMIN) is None
        assert (await _db_settings(session)).booking_confirmation_text == "Ждём вас, {client_name}! {service}, {date} {time}"
        assert "Ждём вас, Анна!" in h.tg.sent_to(ADMIN)[-1]  # предпросмотр на примере

        await h.press(ADMIN, cb("text_reset", field))
        assert (await _db_settings(session)).booking_confirmation_text is None


async def test_text_edit_with_forged_field_is_refused(session):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("text_edit", "currency"))
        await h.press(ADMIN, cb("text", "master_id"))
        await h.press(ADMIN, cb("text_reset", "slot_interval_minutes"))
        assert len(h.tg.alerts()) == 3
        assert await h.get_state(ADMIN) is None


async def test_settings_stay_after_cancel_and_second_text_replaces_first(session):
    await set_owner(session, ADMIN)
    field = "cancellation_text"
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("text_edit", field))
        await h.press(ADMIN, cb("cancel"))
        assert await h.get_state(ADMIN) is None
        assert (await _db_settings(session)).cancellation_text is None
        await h.press(ADMIN, cb("text_edit", field))
        await h.send_text(ADMIN, "Первый")
        await h.press(ADMIN, cb("text_edit", field))
        await h.send_text(ADMIN, "Второй {service}")
    assert (await _db_settings(session)).cancellation_text == "Второй {service}"


# ---------- интерфейс клиента ----------


async def _confirm(h, world, hour=17):
    await h.set_state(CLIENT, BookingFSM.confirm, service_id=world["service_id"], selected_time=at(hour).isoformat())
    await h.press(CLIENT, ConfirmCallback(action="yes"))


async def test_confirmation_text_defaults_are_unchanged(session, world):
    async with tg_harness(client_booking.router) as h:
        await _confirm(h, world)
    text = "\n".join(h.tg.edited())
    assert "✅ Запись подтверждена!" in text and "💰 40 BYN" in text
    assert "Напомним о ней за 24 и за 2 часа 🙂" in text


async def test_currency_shows_in_confirmation_and_master_notification(session, world):
    await update_settings(session, currency="RUB")
    async with tg_harness(client_booking.router) as h:
        await _confirm(h, world)
        assert any("💰 40 RUB" in t for t in h.tg.edited())
        assert any("💰 40 RUB" in t for t in h.tg.sent_to(ADMIN))
        assert not any("BYN" in t for t in h.tg.texts())


async def test_confirmation_omits_reminder_line_when_reminders_are_off(session, world):
    await update_settings(session, reminder_24h_enabled=False, reminder_2h_enabled=False)
    async with tg_harness(client_booking.router) as h:
        await _confirm(h, world)
    assert not any("Напомним" in t for t in h.tg.edited())


async def test_confirmation_mentions_only_enabled_reminder(session, world):
    await update_settings(session, reminder_2h_enabled=False)
    async with tg_harness(client_booking.router) as h:
        await _confirm(h, world)
    assert any("Напомним о ней за 24 часа 🙂" in t for t in h.tg.edited())


async def test_custom_confirmation_text_is_used_and_html_is_escaped(session, world):
    await update_settings(session, booking_confirmation_text="Ждём, {client_name}! <b>{service}</b> {date} в {time}. {address}")
    async with tg_harness(client_booking.router) as h:
        await _confirm(h, world)
    shown = h.tg.edited()[-1]
    session.expire_all()
    name = (await session.get(User, world["user_id"])).first_name  # имя клиента хендлер берёт из БД после обновления
    assert shown.startswith(f"Ждём, {name}! &lt;b&gt;Маникюр&lt;/b&gt;")
    assert at(17).strftime("%d.%m.%Y") in shown and "<" not in shown
    assert "Запись подтверждена" not in shown


async def test_broken_stored_template_falls_back_to_default(session, world):
    # В БД (например, после ручной правки) оказался шаблон с неизвестной подстановкой: клиент всё равно получает текст.
    await update_settings(session, booking_confirmation_text="{evil.__class__}")
    async with tg_harness(client_booking.router, error_middleware=True) as h:
        await _confirm(h, world)
    assert any("✅ Запись подтверждена!" in t for t in h.tg.edited())


async def test_custom_cancellation_text(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    booking = await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    await update_settings(session, cancellation_text="Отменили: {service} {date}")
    async with tg_harness(client_my_booking.router) as h:
        await h.press(CLIENT, BookingActionCallback(booking_id=booking.id, action="cancel_confirm"))
    assert h.tg.edited()[-1] == f"Отменили: Маникюр {at(15).strftime('%d.%m.%Y')}"


async def test_default_cancellation_text_and_currency(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    booking = await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    await update_settings(session, currency="EUR")
    async with tg_harness(client_my_booking.router) as h:
        await h.press(CLIENT, BookingActionCallback(booking_id=booking.id, action="cancel_confirm"))
    assert h.tg.edited()[-1].startswith("Запись отменена.") and "💰 40 EUR" in h.tg.edited()[-1]


async def test_reschedule_is_refused_when_disabled(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    booking = await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    booking_id = booking.id  # до expire_all: потом обращение к booking лезло бы в БД вне greenlet
    await update_settings(session, allow_reschedule=False)
    async with tg_harness(client_my_booking.router, reminder_actions.router) as h:
        await h.press(CLIENT, BookingActionCallback(booking_id=booking.id, action="reschedule"))
        await h.press(CLIENT, ReminderActionCallback(booking_id=booking.id, action="reschedule"))
        assert len(h.tg.alerts()) == 2 and all("отключил перенос" in a for a in h.tg.alerts())
        assert await h.get_state(CLIENT) is None
    session.expire_all()
    assert (await session.get(Booking, booking_id)).start_datetime == at(15)


async def test_reschedule_still_works_by_default(session, world):
    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    booking = await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    async with tg_harness(client_my_booking.router) as h:
        await h.press(CLIENT, BookingActionCallback(booking_id=booking.id, action="reschedule"))
        assert not h.tg.alerts()
        assert await h.get_state(CLIENT) == RescheduleFSM.select_date.state


async def test_reschedule_started_before_disabling_cannot_finish(session, world):
    from app.bot.keyboards.callback_data import TimeCallback

    service = await booking_service_for(session, RIGA_TZ, await tenant_for(session))
    booking = await service.create_booking(world["user_id"], world["service_id"], at(15))
    await session.commit()
    booking_id = booking.id  # до expire_all: потом обращение к booking лезло бы в БД вне greenlet
    await update_settings(session, allow_reschedule=False)
    async with tg_harness(client_my_booking.router) as h:
        await h.set_state(CLIENT, RescheduleFSM.select_time, booking_id=booking.id, service_duration=60)
        await h.press(CLIENT, TimeCallback(ts=int(at(17).timestamp())))
        assert await h.get_state(CLIENT) is None
        assert any("отключил перенос" in a for a in h.tg.alerts())
    session.expire_all()
    assert (await session.get(Booking, booking_id)).start_datetime == at(15)


async def test_reviews_cannot_be_written_when_disabled_but_can_be_read(session, world):
    await update_settings(session, allow_reviews=False)
    async with tg_harness(client_content.router) as h:
        await h.press(CLIENT, ReviewsCallback(action="add"))
        assert any("отключил возможность оставлять отзывы" in t for t in h.tg.sent_to(CLIENT))
        assert await h.get_state(CLIENT) is None
        await h.send_text(CLIENT, "⭐ Отзывы")
        keyboards = [m.reply_markup for m in h.tg.requests if getattr(m, "reply_markup", None) is not None and hasattr(m.reply_markup, "inline_keyboard")]
        labels = [b.text for k in keyboards for row in k.inline_keyboard for b in row]
        assert "📖 Посмотреть" in labels and "🖊️ Оставить" not in labels


async def test_reviews_work_by_default(session, world):
    async with tg_harness(client_content.router) as h:
        await h.press(CLIENT, ReviewsCallback(action="add"))
        assert not any("отключил" in t for t in h.tg.sent_to(CLIENT))


async def test_price_list_uses_master_currency(session, world):
    await update_settings(session, currency="USD")
    async with tg_harness(client_content.router) as h:
        await h.send_text(CLIENT, "💰 Услуги и цены")
    assert any("💰 40 USD" in t for t in h.tg.sent_to(CLIENT))


async def test_other_masters_client_sees_default_currency(session, world, test_user):
    await add_master(session, OTHER_MASTER, OTHER_OWNER)
    other_service = Service(master_id=OTHER_MASTER, name="Стрижка", price=20, duration_minutes=60, active=True, sort_order=0)
    session.add(other_service)
    await session.commit()
    await update_settings(session, MASTER_ID, currency="USD")
    async with tg_harness(client_content.router, master_id=OTHER_MASTER) as h:
        await h.send_text(CLIENT, "💰 Услуги и цены")
    assert any("💰 20 BYN" in t for t in h.tg.sent_to(CLIENT))
