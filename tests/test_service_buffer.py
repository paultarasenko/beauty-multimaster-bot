"""Свободное время после услуги: подготовка к следующему клиенту, недоступна для записи."""

import datetime as dt

import pytest
from aiogram.types import InlineKeyboardMarkup

from app.bot.handlers.admin.services import _buffer_keyboard, _service_card_keyboard, _service_card_text
from app.database.models import Service, WorkingHours
from app.services.booking_service import BookingService
from app.services.exceptions import SlotNotAvailableError
from app.utils.service_input import BUFFER_PRESETS
from tests.conftest import MASTER_ID, next_weekday

TZ = dt.timezone(dt.timedelta(hours=2))
FRIDAY = next_weekday(4)


def at(hhmm: str) -> dt.datetime:
    return dt.datetime.combine(FRIDAY, dt.time.fromisoformat(hhmm), tzinfo=TZ)


async def _prepare(session, buffer_minutes: int) -> Service:
    session.add(WorkingHours(master_id=MASTER_ID, weekday=FRIDAY.weekday(), is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0)))
    service = Service(master_id=MASTER_ID, name="Маникюр", price=40, duration_minutes=60, buffer_minutes=buffer_minutes, active=True)
    session.add(service)
    await session.commit()
    return service


def test_service_occupied_minutes():
    assert Service(master_id=MASTER_ID, duration_minutes=60, buffer_minutes=15).occupied_minutes == 75
    assert Service(master_id=MASTER_ID, duration_minutes=60, buffer_minutes=0).occupied_minutes == 60
    assert Service(master_id=MASTER_ID, duration_minutes=60, buffer_minutes=None).occupied_minutes == 60


def test_card_has_free_time_button_right_of_toggle():
    service = Service(master_id=MASTER_ID, id=7, name="Френч", price=95, duration_minutes=90, buffer_minutes=0, active=True)
    first_row = _service_card_keyboard(service).inline_keyboard[0]
    assert [b.text for b in first_row] == ["🚫 Выключить", "⏳ Свободное время"]


def test_card_text_shows_free_time_only_when_set():
    service = Service(master_id=MASTER_ID, name="Френч", price=95, duration_minutes=90, buffer_minutes=0, active=True)
    assert "Свободное время" not in _service_card_text(service)
    service.buffer_minutes = 15
    assert "Свободное время после услуги: 15 мин" in _service_card_text(service)


def test_buffer_keyboard_layout_matches_design():
    kb: InlineKeyboardMarkup = _buffer_keyboard(7)
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [
        ["0"],
        ["5", "10"],
        ["15", "30"],
        ["⬅️ Назад", "60"],
    ]
    assert BUFFER_PRESETS == (0, 5, 10, 15, 30, 60)


@pytest.mark.asyncio
async def test_buffer_blocks_next_client_start(session, test_user, test_user_2):
    service = await _prepare(session, buffer_minutes=30)
    svc = BookingService(session, TZ, master_id=MASTER_ID)
    await svc.create_booking(test_user.id, service.id, at("12:00"))  # 12:00–13:00 + 30 мин подготовки
    await session.commit()

    slots = await svc.get_available_slots(FRIDAY, service.occupied_minutes)
    assert at("13:00") not in slots  # подготовка идёт до 13:30
    assert at("13:30") in slots
    # Услуга с буфером не должна заходить на уже занятое: 10:30 + 90 мин = 12:00 — последний подходящий старт.
    assert at("10:30") in slots
    assert at("11:00") not in slots

    with pytest.raises(SlotNotAvailableError):
        await svc.create_booking(test_user_2.id, service.id, at("13:00"))


@pytest.mark.asyncio
async def test_no_buffer_keeps_old_behaviour(session, test_user):
    service = await _prepare(session, buffer_minutes=0)
    svc = BookingService(session, TZ, master_id=MASTER_ID)
    await svc.create_booking(test_user.id, service.id, at("12:00"))
    await session.commit()

    slots = await svc.get_available_slots(FRIDAY, service.occupied_minutes)
    assert at("13:00") in slots


@pytest.mark.asyncio
async def test_buffer_is_hidden_from_schedule_check(session, test_user):
    """«Проверить время»: свободное время после услуги показывается как занятое."""
    service = await _prepare(session, buffer_minutes=15)
    svc = BookingService(session, TZ, master_id=MASTER_ID)
    await svc.create_booking(test_user.id, service.id, at("12:00"))
    await session.commit()

    _, free, busy = await svc.get_day_schedule(FRIDAY)
    assert (at("13:00"), at("13:15")) not in [(i.start, i.end) for i in free]
    assert any(i.start == at("13:15") for i in free)
    assert busy[0].end == at("13:15")


@pytest.mark.asyncio
async def test_booking_snapshots_buffer_and_old_booking_ignores_later_change(session, test_user):
    service = await _prepare(session, buffer_minutes=30)
    svc = BookingService(session, TZ, master_id=MASTER_ID)
    booking = await svc.create_booking(test_user.id, service.id, at("12:00"))
    await session.commit()
    assert booking.service_buffer_snapshot == 30
    assert booking.end_datetime == at("13:00")  # в записи и уведомлениях время услуги без буфера

    service.buffer_minutes = 0  # мастер убрал свободное время позже
    await session.commit()
    slots = await svc.get_available_slots(FRIDAY, service.occupied_minutes)
    assert at("13:00") not in slots  # старая запись сохранила свои 30 минут
    assert at("13:30") in slots


@pytest.mark.asyncio
async def test_buffer_tail_of_previous_booking_is_found_by_repo(session, test_user):
    """Запись закончилась до начала диапазона, но её подготовка заходит в него."""
    service = await _prepare(session, buffer_minutes=30)
    svc = BookingService(session, TZ, master_id=MASTER_ID)
    await svc.create_booking(test_user.id, service.id, at("12:00"))
    await session.commit()

    found = await svc.bookings.get_confirmed_busy_in_range(at("13:10"), at("14:00"))
    assert len(found) == 1
    assert await svc.bookings.get_confirmed_in_range(at("13:10"), at("14:00")) == []
