"""Подменю «Записи»: текст сообщения, с которым показываются кнопки."""

import pytest_asyncio
from aiogram.methods import SendMessage

from app.bot.handlers.admin import block_time, bookings
from app.bot.keyboards.admin_keyboards import BOOKINGS_MENU_PROMPT, BTN_BOOKINGS_WEEK, admin_bookings_menu_keyboard
from app.bot.keyboards.callback_data import NavCallback
from app.bot.states.booking_states import AdminBlockTimeFSM
from tests.tg import tg_harness
from tests.conftest import set_owner

ADMIN_ID = 999


@pytest_asyncio.fixture(autouse=True)
async def _admin(session):
    await set_owner(session, ADMIN_ID)


def _last_sent(h) -> SendMessage:
    return [m for m in h.tg.requests if isinstance(m, SendMessage)][-1]


def _button_texts(markup) -> list[str]:
    return [b.text for row in markup.keyboard for b in row]


def test_prompt_is_not_the_text_of_the_menu_button():
    """Кнопка «📅 Записи» открывает это меню, а бот перехватывает её по тексту: ответ не должен совпадать с кнопкой."""
    assert BOOKINGS_MENU_PROMPT == "Выберите действие"


async def test_pressing_bookings_button_shows_submenu_with_new_prompt(session):
    async with tg_harness(bookings.router) as h:
        await h.send_text(ADMIN_ID, "📅 Записи")

        sent = _last_sent(h)
        assert sent.text == "Выберите действие"
        assert _button_texts(sent.reply_markup) == _button_texts(admin_bookings_menu_keyboard())
        assert BTN_BOOKINGS_WEEK in _button_texts(sent.reply_markup)


async def test_answer_to_the_prompt_does_not_reopen_the_menu(session):
    """Если бы ответ совпадал с текстом кнопки, он снова попал бы в этот же обработчик."""
    async with tg_harness(bookings.router) as h:
        await h.send_text(ADMIN_ID, "📅 Записи")
        assert len(h.tg.sent_to(ADMIN_ID)) == 1
        await h.send_text(ADMIN_ID, BOOKINGS_MENU_PROMPT)
        assert h.tg.sent_to(ADMIN_ID) == ["Выберите действие"], "обычный текст не должен открывать меню повторно"


async def test_back_from_block_time_shows_submenu_with_the_same_prompt(session):
    async with tg_harness(block_time.router) as h:
        await h.set_state(ADMIN_ID, AdminBlockTimeFSM.select_date)
        await h.press(ADMIN_ID, NavCallback(target="back"))

        sent = _last_sent(h)
        assert sent.text == BOOKINGS_MENU_PROMPT
        assert _button_texts(sent.reply_markup) == _button_texts(admin_bookings_menu_keyboard())
        assert await h.get_state(ADMIN_ID) is None
