"""Список «Отменить запись»: по 10 записей на странице, кнопки «◀️ Раньше» / «Дальше ➡️», возврат на ту же страницу."""

import datetime as dt

import pytest_asyncio
from aiogram.methods import EditMessageText, SendMessage

from app.bot.handlers.admin import bookings as admin_bookings
from app.bot.keyboards.admin_keyboards import BTN_BOOKINGS_DELETE
from app.bot.keyboards.callback_data import AdminBookingCallback, AdminBookingsPageCallback
from app.database.models import Booking
from tests.tg import tg_harness
from tests.conftest import MASTER_ID, set_owner

ADMIN_ID = 4242
PREV, NEXT = "◀️ Раньше", "Дальше ➡️"


@pytest_asyncio.fixture(autouse=True)
async def _admin(session):
    await set_owner(session, ADMIN_ID)


async def _make_bookings(session, user, service, count: int) -> list[Booking]:
    """count подтверждённых будущих записей, по возрастанию времени, с зазором (не пересекаются)."""
    base = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    bookings = []
    for i in range(count):
        start = base + dt.timedelta(hours=3 * i)
        booking = Booking(master_id=MASTER_ID,
            user_id=user.id,
            service_id=service.id,
            start_datetime=start,
            end_datetime=start + dt.timedelta(minutes=service.duration_minutes),
            status="confirmed",
            service_name_snapshot=service.name,
            service_price_snapshot=service.price,
            service_duration_snapshot=service.duration_minutes,
        )
        session.add(booking)
        bookings.append(booking)
    await session.commit()
    return bookings


def _last_markup(h):
    edits_and_sends = [m for m in h.tg.requests if isinstance(m, (SendMessage, EditMessageText))]
    return edits_and_sends[-1].reply_markup


def _booking_buttons(markup) -> list:
    return [row[0] for row in markup.inline_keyboard if row[0].callback_data.startswith("abk:")]


def _nav_texts(markup) -> list[str]:
    last = markup.inline_keyboard[-1]
    return [b.text for b in last] if last[0].callback_data.startswith("abkp:") else []


def _booking_ids(markup) -> list[int]:
    return [AdminBookingCallback.unpack(b.callback_data).booking_id for b in _booking_buttons(markup)]


async def test_few_bookings_have_no_navigation(session, master_profile, test_user, manicure_service):
    await _make_bookings(session, test_user, manicure_service, 10)
    async with tg_harness(admin_bookings.router) as h:
        await h.send_text(ADMIN_ID, BTN_BOOKINGS_DELETE)
        markup = _last_markup(h)
    assert len(_booking_buttons(markup)) == 10
    assert _nav_texts(markup) == []


async def test_no_bookings(session, master_profile):
    async with tg_harness(admin_bookings.router) as h:
        await h.send_text(ADMIN_ID, BTN_BOOKINGS_DELETE)
    assert h.tg.texts()[-1] == "Предстоящих записей нет."


async def test_pages_follow_each_other_in_time_order(session, master_profile, test_user, manicure_service):
    created = await _make_bookings(session, test_user, manicure_service, 25)
    ids = [b.id for b in created]  # уже по возрастанию времени

    async with tg_harness(admin_bookings.router) as h:
        await h.send_text(ADMIN_ID, BTN_BOOKINGS_DELETE)
        first = _last_markup(h)
        await h.press(ADMIN_ID, AdminBookingsPageCallback(page=1))
        second = _last_markup(h)
        await h.press(ADMIN_ID, AdminBookingsPageCallback(page=2))
        third = _last_markup(h)

    # Первая: только «Дальше»; средняя: обе; последняя: только «Раньше».
    assert _nav_texts(first) == [NEXT]
    assert _nav_texts(second) == [PREV, NEXT]
    assert _nav_texts(third) == [PREV]
    # Записи не теряются и не повторяются, порядок по времени.
    assert _booking_ids(first) == ids[:10]
    assert _booking_ids(second) == ids[10:20]
    assert _booking_ids(third) == ids[20:25]


async def test_navigation_buttons_point_to_neighbour_pages(session, master_profile, test_user, manicure_service):
    await _make_bookings(session, test_user, manicure_service, 25)
    async with tg_harness(admin_bookings.router) as h:
        await h.press(ADMIN_ID, AdminBookingsPageCallback(page=1))
        nav = _last_markup(h).inline_keyboard[-1]
    assert AdminBookingsPageCallback.unpack(nav[0].callback_data).page == 0
    assert AdminBookingsPageCallback.unpack(nav[1].callback_data).page == 2


async def test_back_from_confirm_returns_to_same_page(session, master_profile, test_user, manicure_service):
    created = await _make_bookings(session, test_user, manicure_service, 25)
    ids = [b.id for b in created]

    async with tg_harness(admin_bookings.router) as h:
        await h.press(ADMIN_ID, AdminBookingsPageCallback(page=1))
        opened = _booking_buttons(_last_markup(h))[0]  # первая запись второй страницы
        await h.press(ADMIN_ID, opened.callback_data)
        confirm_markup = _last_markup(h)
        back = confirm_markup.inline_keyboard[0][1]
        assert back.text == "⬅️ Назад"
        await h.press(ADMIN_ID, back.callback_data)
        returned = _last_markup(h)

    assert _booking_ids(returned) == ids[10:20]


async def test_old_buttons_without_page_still_work(session, master_profile, test_user, manicure_service):
    """Кнопки, отправленные до появления страниц (без номера страницы), открывают запись и ведут «Назад» на первую."""
    created = await _make_bookings(session, test_user, manicure_service, 3)
    async with tg_harness(admin_bookings.router) as h:
        await h.press(ADMIN_ID, f"abk:{created[0].id}:delete")
        confirm_markup = _last_markup(h)
        back = confirm_markup.inline_keyboard[0][1]
        await h.press(ADMIN_ID, back.callback_data)
        returned = _last_markup(h)
    assert _booking_ids(returned) == [b.id for b in created]


async def test_page_past_the_end_shows_last_page(session, master_profile, test_user, manicure_service):
    """Записи удалили, пока мастер листал: пустая страница заменяется последней существующей."""
    created = await _make_bookings(session, test_user, manicure_service, 12)
    async with tg_harness(admin_bookings.router) as h:
        await h.press(ADMIN_ID, AdminBookingsPageCallback(page=5))
        markup = _last_markup(h)
    assert _booking_ids(markup) == [b.id for b in created][10:12]
    assert _nav_texts(markup) == [PREV]


async def test_cancelled_and_past_bookings_are_not_listed(session, master_profile, test_user, manicure_service):
    created = await _make_bookings(session, test_user, manicure_service, 12)
    created[0].status = "cancelled"
    await session.commit()
    async with tg_harness(admin_bookings.router) as h:
        await h.send_text(ADMIN_ID, BTN_BOOKINGS_DELETE)
        markup = _last_markup(h)
    # 11 живых записей: 10 на первой странице, и «Дальше» есть.
    assert _booking_ids(markup) == [b.id for b in created][1:11]
    assert _nav_texts(markup) == [NEXT]
