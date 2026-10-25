"""Ввод мастера при добавлении/редактировании услуги: одни и те же правила в обоих сценариях."""

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.admin import services as admin_services
from app.bot.keyboards.callback_data import AdminDurationCallback
from app.bot.states.booking_states import AdminServiceFSM
from app.database.models import Service
from app.utils.service_input import (
    InvalidServiceInput,
    parse_description,
    parse_duration,
    parse_name,
    parse_price,
)
from tests.tg import tg_harness
from tests.conftest import set_owner

ADMIN_ID = 999

# --- чистые функции разбора ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("40", "40.00"), ("40,5", "40.50"), ("40.50", "40.50"), ("0", "0.00"), (" 25 ", "25.00"),
     ("0.005", "0.01"), ("99999999", "99999999.00")],
)
def test_parse_price_accepts_plain_decimals(raw, expected):
    assert parse_price(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "nan", "NaN", "inf", "-inf", "Infinity", "-1", "-0.5", "1e3", "1_000", "40 BYN",
     "40.5.1", "4,0,0", "100000000", "1e12", "٤٠", "①"],
)
def test_parse_price_rejects_non_prices(raw):
    with pytest.raises(InvalidServiceInput):
        parse_price(raw)


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("60", 60), (" 90 ", 90), ("1440", 1440)])
def test_parse_duration_accepts_whole_minutes(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 20", 80),
        ("2 00", 120),
        ("24 00", 1440),
        ("1 5", 65),
        ("1ч20", 80),
        ("1 ч 20", 80),
        ("1:20", 80),
        ("01:05", 65),
        ("24:00", 1440),
        ("1 ч 20 мин", 80),
        ("1ч20м", 80),
        ("1 час 20 минут", 80),
        ("2 часа", 120),
        ("2 ч", 120),
        ("3 часов", 180),
        ("45 мин", 45),
        ("60 минут", 60),
        ("90м", 90),
        (" 1 Ч 5 МИН ", 65),
        ("1 ч. 30 мин.", 90),
    ],
)
def test_parse_duration_accepts_hours_and_minutes(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None, "", "0", "-5", "1441", "10000000000", "1.5", "abc", "²", "٦٠", "00000",
        "1:60", "25:00", "24:01", "1:5", "0 мин", "0 ч", "25 ч", "1 ч 90 мин", "ч", "мин", "20 мин 1 ч", "1 60", "0 0", "25 00", "120 20", "1 20 мин",
    ],
)
def test_parse_duration_rejects_garbage_and_out_of_range(raw):
    with pytest.raises(InvalidServiceInput):
        parse_duration(raw)


def test_parse_name_strips_and_enforces_column_length():
    assert parse_name("  Маникюр  ") == "Маникюр"
    assert parse_name("x" * 255) == "x" * 255
    for bad in (None, "", "   ", "x" * 256):
        with pytest.raises(InvalidServiceInput):
            parse_name(bad)


@pytest.mark.parametrize("raw", ["-", "нет", "Нет", " НЕТ "])
def test_parse_description_clear_words_return_none(raw):
    assert parse_description(raw) is None


def test_parse_description_rules():
    assert parse_description("  Классический  ") == "Классический"
    for bad in (None, "", "  ", "<b>жирный</b>", "a > b", "x" * 1001):
        with pytest.raises(InvalidServiceInput):
            parse_description(bad)


# --- сквозные сценарии: настоящий диспетчер и БД ---

BAD_PRICES = ["nan", "inf", "-1", "1e12", "100000000", "abc", "", None]
BAD_DURATIONS = ["0", "-5", "1441", "10000000000", "1.5", "²", "abc", None]


async def _services(session) -> list[Service]:
    session.expire_all()
    return list((await session.execute(select(Service).order_by(Service.id))).scalars())


@pytest_asyncio.fixture(autouse=True)
async def _admin(session):
    await set_owner(session, ADMIN_ID)


async def test_add_service_full_flow_creates_service(session):
    async with tg_harness(admin_services.router) as h:
        await h.press(ADMIN_ID, "svc_add")
        await h.send_text(ADMIN_ID, "Педикюр")
        await h.send_text(ADMIN_ID, "55,5")
        await h.send_text(ADMIN_ID, "75")
        await h.send_text(ADMIN_ID, "нет")
        assert await h.get_state(ADMIN_ID) is None

    [service] = await _services(session)
    assert (service.name, service.price, service.duration_minutes, service.description, service.active) == (
        "Педикюр", Decimal("55.50"), 75, None, True,
    )


@pytest.mark.parametrize("bad", BAD_PRICES)
async def test_add_service_rejects_bad_price_and_keeps_asking(session, bad):
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_price, name="Педикюр")
        await h.send_text(ADMIN_ID, bad)
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_price.state
        assert h.tg.sent_to(ADMIN_ID)[-1] == "Введите цену числом, например 40 или 40.50"
    assert await _services(session) == []


@pytest.mark.parametrize("bad", BAD_DURATIONS)
async def test_add_service_rejects_bad_duration_and_keeps_asking(session, bad):
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_duration, name="Педикюр", price="40.00")
        await h.send_text(ADMIN_ID, bad)
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_duration.state
    assert await _services(session) == []


@pytest.mark.parametrize("bad", [None, "", "   ", "x" * 256])
async def test_add_service_rejects_bad_name(session, bad):
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_name)
        await h.send_text(ADMIN_ID, bad)
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_name.state


@pytest.mark.parametrize("bad", ["<b>x</b>", "x" * 1001])
async def test_add_service_rejects_bad_description(session, bad):
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_description, name="Педикюр", price="40.00", duration=60)
        await h.send_text(ADMIN_ID, bad)
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_description.state
    assert await _services(session) == []


@pytest.mark.parametrize("bad", BAD_PRICES)
async def test_edit_price_rejects_the_same_values_as_add(session, manicure_service, bad):
    service_id = manicure_service.id
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.edit_price, service_id=service_id)
        await h.send_text(ADMIN_ID, bad)
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.edit_price.state
    [service] = await _services(session)
    assert service.price == Decimal("40.00")


@pytest.mark.parametrize("bad", BAD_DURATIONS)
async def test_edit_duration_rejects_the_same_values_as_add(session, manicure_service, bad):
    service_id = manicure_service.id
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.edit_duration, service_id=service_id)
        await h.send_text(ADMIN_ID, bad)
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.edit_duration.state
    [service] = await _services(session)
    assert service.duration_minutes == 60


async def test_edit_flows_save_valid_values(session, manicure_service):
    service_id = manicure_service.id
    async with tg_harness(admin_services.router) as h:
        for state, text in [
            (AdminServiceFSM.edit_name, "Гель-лак"),
            (AdminServiceFSM.edit_price, "45,5"),
            (AdminServiceFSM.edit_duration, "80"),
            (AdminServiceFSM.edit_description, "С покрытием"),
        ]:
            await h.set_state(ADMIN_ID, state, service_id=service_id)
            await h.send_text(ADMIN_ID, text)
            assert await h.get_state(ADMIN_ID) is None
    [service] = await _services(session)
    assert (service.name, service.price, service.duration_minutes, service.description) == (
        "Гель-лак", Decimal("45.50"), 80, "С покрытием",
    )


# --- кнопки быстрого выбора длительности ---


def test_duration_keyboard_has_presets_three_per_row():
    kb = admin_services._duration_keyboard(0)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == [
        "15 мин", "30 мин", "45 мин",
        "1 ч", "1 ч 15 мин", "1 ч 30 мин",
        "1 ч 45 мин", "2 ч", "2 ч 15 мин",
        "2 ч 30 мин", "2 ч 45 мин", "3 ч",
    ]
    assert all(len(row) == 3 for row in kb.inline_keyboard)


def test_duration_keyboard_cancel_only_when_editing():
    assert len(admin_services._duration_keyboard(0).inline_keyboard) == 4
    edit_rows = admin_services._duration_keyboard(5).inline_keyboard
    assert len(edit_rows) == 5 and edit_rows[-1][0].text == "⬅️ Отмена"


async def test_add_service_duration_button_then_description(session):
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_duration, name="Педикюр", price="40.00")
        await h.press(ADMIN_ID, AdminDurationCallback(service_id=0, minutes=75))
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_description.state
        assert h.tg.edited() == ["🕐 Длительность: 1 ч 15 мин"]
        await h.send_text(ADMIN_ID, "нет")
        assert await h.get_state(ADMIN_ID) is None
    [service] = await _services(session)
    assert (service.name, service.duration_minutes) == ("Педикюр", 75)


async def test_add_service_stale_duration_button_is_ignored(session):
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_description, name="Педикюр", price="40.00", duration=60)
        await h.press(ADMIN_ID, AdminDurationCallback(service_id=0, minutes=15))
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_description.state
        assert (await h.fsm(ADMIN_ID).get_data())["duration"] == 60


async def test_edit_duration_button_saves_and_clears_state(session, manicure_service):
    service_id = manicure_service.id
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.edit_duration, service_id=service_id)
        await h.press(ADMIN_ID, AdminDurationCallback(service_id=service_id, minutes=180))
        assert await h.get_state(ADMIN_ID) is None
        assert h.tg.edited()[-1].startswith("✅ Длительность изменена.")
        assert "🕐 3 ч" in h.tg.edited()[-1]
    [service] = await _services(session)
    assert service.duration_minutes == 180


async def test_edit_duration_old_button_does_not_break_other_input(session, manicure_service):
    service_id = manicure_service.id
    async with tg_harness(admin_services.router) as h:
        await h.set_state(ADMIN_ID, AdminServiceFSM.enter_name)
        await h.press(ADMIN_ID, AdminDurationCallback(service_id=service_id, minutes=30))
        assert await h.get_state(ADMIN_ID) == AdminServiceFSM.enter_name.state
    [service] = await _services(session)
    assert service.duration_minutes == 30


async def test_edit_duration_button_rejects_value_outside_presets(session, manicure_service):
    service_id = manicure_service.id
    async with tg_harness(admin_services.router) as h:
        await h.press(ADMIN_ID, AdminDurationCallback(service_id=service_id, minutes=7))
    [service] = await _services(session)
    assert service.duration_minutes == 60
