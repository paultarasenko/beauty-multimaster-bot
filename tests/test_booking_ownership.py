"""Клиент не должен видеть, отменять и переносить чужие записи, подставив чужой booking_id в callback_data.

callback_data приходит от клиента Telegram и может быть подделана — доверять можно только проверке владельца на сервере.
"""

import datetime as dt

import pytest
import pytest_asyncio

from app.bot.handlers.client import my_booking, reminder_actions
from app.bot.keyboards.callback_data import BookingActionCallback, ReminderActionCallback
from app.bot.states.booking_states import RescheduleFSM
from app.database.models import Booking, BookingStatus, Notification, NotificationStatus, NotificationType, WorkingHours
from app.services.booking_service import BookingService
from tests.conftest import MASTER_ID, RIGA_TZ, next_weekday, set_owner
from tests.tg import tg_harness

ADMIN_ID = 999
OWNER_TG = 111111  # test_user
STRANGER_TG = 222222  # test_user_2


@pytest_asyncio.fixture
async def owners_booking(session, master_profile, test_user, test_user_2, manicure_service) -> Booking:
    await set_owner(session, ADMIN_ID)
    day = next_weekday(4)
    session.add(WorkingHours(master_id=MASTER_ID, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    await session.commit()
    start = dt.datetime.combine(day, dt.time(15, 0), tzinfo=RIGA_TZ)
    booking = await BookingService(session, RIGA_TZ, master_id=MASTER_ID).create_booking(test_user.id, manicure_service.id, start)
    await session.commit()
    return booking


async def _reload(session, booking_id: int) -> Booking:
    session.expire_all()
    return await session.get(Booking, booking_id)


@pytest.mark.parametrize("action", ["cancel", "cancel_back", "cancel_confirm", "reschedule"])
async def test_stranger_cannot_use_my_booking_buttons_on_foreign_booking(session, owners_booking, action):
    booking_id = owners_booking.id
    async with tg_harness(my_booking.router) as h:
        await h.press(STRANGER_TG, BookingActionCallback(booking_id=booking_id, action=action))

        assert h.tg.alerts(), "чужому пользователю нужно ответить отказом"
        assert h.tg.edited() == [], "карточка чужой записи не должна показываться"
        assert all("Маникюр" not in t for t in h.tg.texts())
        assert h.tg.sent_to(ADMIN_ID) == [], "мастеру не должно уйти уведомление об отмене"
        assert await h.get_state(STRANGER_TG) is None, "перенос чужой записи не должен начинаться"

    assert (await _reload(session, booking_id)).status == BookingStatus.CONFIRMED.value


async def test_owner_can_cancel_own_booking_and_master_is_notified(session, owners_booking):
    booking_id = owners_booking.id
    async with tg_harness(my_booking.router) as h:
        await h.press(OWNER_TG, BookingActionCallback(booking_id=booking_id, action="cancel_confirm"))

        assert h.tg.alerts() == []
        assert any("Запись отменена" in t for t in h.tg.edited())
        assert any("Запись отменена" in t for t in h.tg.sent_to(ADMIN_ID))

    assert (await _reload(session, booking_id)).status == BookingStatus.CANCELLED.value


async def test_owner_can_start_reschedule_of_own_booking(session, owners_booking):
    async with tg_harness(my_booking.router) as h:
        await h.press(OWNER_TG, BookingActionCallback(booking_id=owners_booking.id, action="reschedule"))

        assert await h.get_state(OWNER_TG) == RescheduleFSM.select_date.state
        assert h.tg.alerts() == []


async def test_unknown_booking_id_gets_not_found_alert_not_a_crash(session, owners_booking):
    async with tg_harness(my_booking.router, error_middleware=True) as h:
        await h.press(OWNER_TG, BookingActionCallback(booking_id=987654, action="cancel_confirm"))

        assert h.tg.alerts() and "ошло не так" not in h.tg.alerts()[0], "ожидали аккуратный отказ, а не общую ошибку"


@pytest.mark.parametrize("action", ["still_valid", "cancel", "reschedule"])
async def test_stranger_cannot_use_reminder_buttons_on_foreign_booking(session, owners_booking, action):
    booking_id = owners_booking.id
    async with tg_harness(reminder_actions.router) as h:
        await h.press(STRANGER_TG, ReminderActionCallback(booking_id=booking_id, action=action))

        assert h.tg.alerts(), "чужому пользователю нужно ответить отказом"
        assert h.tg.edited() == []
        assert await h.get_state(STRANGER_TG) is None

    session.expire_all()
    reminder_24h = (
        await session.execute(
            Notification.__table__.select().where(
                Notification.booking_id == booking_id, Notification.type == NotificationType.REMINDER_24H.value
            )
        )
    ).one()
    assert reminder_24h.status == NotificationStatus.PENDING.value, "чужое напоминание не должно гаситься"


async def test_owner_still_valid_confirms_only_24h_reminder(session, owners_booking):
    booking_id = owners_booking.id
    async with tg_harness(reminder_actions.router) as h:
        await h.press(OWNER_TG, ReminderActionCallback(booking_id=booking_id, action="still_valid"))
        assert any("Спасибо" in t for t in h.tg.edited())

    session.expire_all()
    rows = (
        await session.execute(
            Notification.__table__.select().where(Notification.booking_id == booking_id)
        )
    ).all()
    by_type = {r.type: r.status for r in rows}
    assert by_type[NotificationType.REMINDER_24H.value] == NotificationStatus.SENT.value
    assert by_type[NotificationType.REMINDER_2H_CLIENT.value] == NotificationStatus.PENDING.value
    assert by_type[NotificationType.REMINDER_2H_MASTER.value] == NotificationStatus.PENDING.value
