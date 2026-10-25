"""Обращение ограниченного клиента к мастеру: «📞 Связаться со мной» → «Открыть доступ» / «Ограничить навсегда»."""

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.admin import access_requests, client_access
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import contact_master
from app.bot.handlers.client import content as client_content
from app.bot.handlers.client import master_request
from app.bot.keyboards.callback_data import (
    AdminAccessRequestCallback,
    AdminClientAccessCallback,
    MasterRequestCallback,
    ReviewsCallback,
)
from app.bot.states.booking_states import ContactMasterFSM
from app.database.models import AccessRequestStatus, ClientAccessRequest, User
from app.services.access_request_service import BLOCKED_TEXT, OPENED_TEXT, PENDING_TEXT
from tests.tg import tg_harness
from tests.conftest import MASTER_ID, client_row, set_client, set_owner

ADMIN_ID = 999
CLIENT_TG = 111111  # test_user
CONTACT = "📞 Связаться со мной"
ROUTERS = (contact_master.router, access_requests.router, client_content.router)


@pytest_asyncio.fixture(autouse=True)
async def _admin(session):
    await set_owner(session, ADMIN_ID)


async def _restrict(session, user, forever: bool = False):
    await set_client(session, user, is_restricted=True, is_blocked_forever=forever)


async def _only_request(session) -> ClientAccessRequest:
    result = await session.execute(select(ClientAccessRequest).execution_options(populate_existing=True))
    return result.scalars().one()


async def _user(session, user_id) -> User:
    result = await session.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    return result.scalars().one()


async def _write(h, text="Простите, это была ошибка"):
    await h.send_text(CLIENT_TG, CONTACT)
    await h.send_text(CLIENT_TG, text)


async def test_unrestricted_client_still_gets_the_contact_link(session, test_user):
    async with tg_harness(*ROUTERS) as h:
        await h.send_text(CLIENT_TG, CONTACT)
        assert h.tg.sent_to(CLIENT_TG) == ["📞 Написать мастеру: @master_test"]
        assert await h.get_state(CLIENT_TG) is None


async def test_restricted_client_writes_one_message_and_master_gets_two_buttons(session, test_user):
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS) as h:
        await h.send_text(CLIENT_TG, CONTACT)
        assert await h.get_state(CLIENT_TG) == ContactMasterFSM.waiting_message.state
        await h.send_text(CLIENT_TG, "Простите, это была ошибка")
        assert await h.get_state(CLIENT_TG) is None
        assert "✅ Сообщение передано мастеру" in h.tg.sent_to(CLIENT_TG)[-1]

        sent = next(r for r in h.tg.requests if getattr(r, "chat_id", None) == ADMIN_ID)
        assert "Простите, это была ошибка" in sent.text and "@client1" in sent.text
        buttons = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
        assert buttons == ["✅ Открыть доступ", "🔒 Ограничить навсегда"]
    assert (await _only_request(session)).status == AccessRequestStatus.PENDING.value


async def test_second_attempt_while_pending_gets_polite_message(session, test_user):
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS) as h:
        await _write(h)
        await h.send_text(CLIENT_TG, CONTACT)
        assert h.tg.sent_to(CLIENT_TG)[-1] == PENDING_TEXT
        assert await h.get_state(CLIENT_TG) is None
    assert len((await session.execute(select(ClientAccessRequest))).scalars().all()) == 1


async def test_open_access_unrestricts_client_and_notifies_him(session, test_user):
    user_id = test_user.id
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS) as h:
        await _write(h)
        req = await _only_request(session)
        await h.press(ADMIN_ID, AdminAccessRequestCallback(request_id=req.id, action="open"))
        assert h.tg.sent_to(CLIENT_TG)[-1] == OPENED_TEXT
        assert "Доступ открыт" in h.tg.edited()[-1]
    client = await client_row(session, user_id)
    assert client.is_restricted is False and client.is_blocked_forever is False
    assert client.cancel_streak_reset_at is not None
    assert (await _only_request(session)).status == AccessRequestStatus.OPENED.value


async def test_block_forever_closes_booking_reviews_and_contact(session, test_user, manicure_service):
    user_id = test_user.id
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS, client_booking.router, master_request.router) as h:
        await _write(h)
        req = await _only_request(session)
        await h.press(ADMIN_ID, AdminAccessRequestCallback(request_id=req.id, action="block"))
        assert h.tg.sent_to(CLIENT_TG)[-1] == BLOCKED_TEXT
        assert "Ограничено навсегда" in h.tg.edited()[-1]
        assert (await client_row(session, user_id)).is_blocked_forever is True

        before = len(h.tg.sent_to(CLIENT_TG))
        await h.send_text(CLIENT_TG, "📅 Записаться")  # запись
        await h.send_text(CLIENT_TG, CONTACT)  # обращение к мастеру
        await h.press(CLIENT_TG, ReviewsCallback(action="add"))  # отзыв
        await h.press(CLIENT_TG, MasterRequestCallback(action="write"))  # «Написать мастеру» при лимите
        assert h.tg.sent_to(CLIENT_TG)[before:] == [BLOCKED_TEXT] * 4
        assert await h.get_state(CLIENT_TG) is None
        assert not any("Выберите услугу" in t for t in h.tg.texts())


async def test_blocked_client_cannot_edit_review(session, test_user):
    await _restrict(session, test_user, forever=True)
    async with tg_harness(client_content.router) as h:
        await h.press(CLIENT_TG, ReviewsCallback(action="edit"))
        assert h.tg.sent_to(CLIENT_TG) == [BLOCKED_TEXT]


async def test_double_press_and_foreign_press_are_harmless(session, test_user):
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS) as h:
        await _write(h)
        req = await _only_request(session)
        await h.press(CLIENT_TG, AdminAccessRequestCallback(request_id=req.id, action="block"))  # не мастер
        assert (await _only_request(session)).status == AccessRequestStatus.PENDING.value

        await h.press(ADMIN_ID, AdminAccessRequestCallback(request_id=req.id, action="open"))
        await h.press(ADMIN_ID, AdminAccessRequestCallback(request_id=req.id, action="block"))
        assert h.tg.alerts() == ["Это обращение уже обработано."]
        assert len([t for t in h.tg.sent_to(CLIENT_TG) if t == OPENED_TEXT]) == 1
    assert (await _only_request(session)).status == AccessRequestStatus.OPENED.value


async def test_too_long_and_non_text_messages_are_not_sent(session, test_user):
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS) as h:
        await h.send_text(CLIENT_TG, CONTACT)
        await h.send_text(CLIENT_TG, "а" * 501)
        assert any("слишком длинное" in t for t in h.tg.sent_to(CLIENT_TG))
        assert await h.get_state(CLIENT_TG) == ContactMasterFSM.waiting_message.state
        assert h.tg.sent_to(ADMIN_ID) == []
    assert (await session.execute(select(ClientAccessRequest))).scalars().all() == []


async def test_menu_button_interrupts_contact_input(session, test_user):
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS, reset_input=True) as h:
        await h.send_text(CLIENT_TG, CONTACT)
        await h.send_text(CLIENT_TG, "💰 Услуги и цены")
        assert await h.get_state(CLIENT_TG) is None
        assert h.tg.sent_to(ADMIN_ID) == []
    assert (await session.execute(select(ClientAccessRequest))).scalars().all() == []


async def test_request_is_dropped_if_master_cannot_be_reached(session, test_user):
    await _restrict(session, test_user)
    async with tg_harness(*ROUTERS) as h:
        h.tg.blocked_chat_ids.add(ADMIN_ID)
        await _write(h)
        assert any("Не удалось передать" in t for t in h.tg.sent_to(CLIENT_TG))
    assert (await session.execute(select(ClientAccessRequest))).scalars().all() == []


async def test_old_allow_button_also_lifts_permanent_block(session, test_user):
    user_id = test_user.id
    await _restrict(session, test_user, forever=True)
    async with tg_harness(client_access.router) as h:
        await h.press(ADMIN_ID, AdminClientAccessCallback(user_id=user_id, action="allow"))
    client = await client_row(session, user_id)
    assert client.is_restricted is False and client.is_blocked_forever is False


async def test_contact_router_wins_over_admin_content_router(session, test_user):
    """В main.py admin/content.py (общие клиентские кнопки) стоит раньше клиентских роутеров и тоже ловит
    «📞 Связаться со мной» — поэтому contact_master подключён самым первым. Порядок здесь такой же."""
    from app.bot.handlers.admin import content as admin_content

    await _restrict(session, test_user)
    async with tg_harness(contact_master.router, admin_content.router, client_content.router) as h:
        await h.send_text(CLIENT_TG, CONTACT)
        assert await h.get_state(CLIENT_TG) == ContactMasterFSM.waiting_message.state
        assert "Напишите сообщение мастеру" in h.tg.sent_to(CLIENT_TG)[-1]


# --- отзывы: ограниченный и заблокированный клиент только читают ---

RESTRICTED_REVIEWS_TEXT = (
    "К сожалению, оставлять и редактировать отзывы сейчас недоступно.\n\n"
    "Вы можете читать отзывы других клиентов."
)


def _buttons(request) -> list[str]:
    return [b.text for row in request.reply_markup.inline_keyboard for b in row]


@pytest.mark.parametrize("forever, denied", [(False, RESTRICTED_REVIEWS_TEXT), (True, BLOCKED_TEXT)])
async def test_restricted_or_blocked_client_can_only_read_reviews(session, test_user, forever, denied):
    from app.database.models import Review

    session.add(Review(master_id=MASTER_ID, author_name="Анна", rating=5, text="Отлично!", active=True))
    await _restrict(session, test_user, forever=forever)
    async with tg_harness(client_content.router) as h:
        await h.send_text(CLIENT_TG, "⭐ Отзывы")
        entry = h.tg.requests[-1]
        assert _buttons(entry) == ["📖 Посмотреть"]  # без «Оставить»

        await h.press(CLIENT_TG, ReviewsCallback(action="view"))
        page = next(r for r in h.tg.requests if "Отлично!" in (getattr(r, "text", None) or ""))
        assert page.reply_markup is None  # читать можно, писать нельзя

        # Старые кнопки в чате (от времени, когда клиент ещё не был ограничен) тоже не работают.
        await h.press(CLIENT_TG, ReviewsCallback(action="add"))
        await h.press(CLIENT_TG, ReviewsCallback(action="edit"))
        assert h.tg.sent_to(CLIENT_TG)[-2:] == [denied, denied]
        assert await h.get_state(CLIENT_TG) is None


async def test_normal_client_keeps_write_button(session, test_user):
    async with tg_harness(client_content.router) as h:
        await h.send_text(CLIENT_TG, "⭐ Отзывы")
        assert _buttons(h.tg.requests[-1]) == ["📖 Посмотреть", "🖊️ Оставить"]


async def test_client_restricted_while_filling_review_form_cannot_finish_it(session, test_user):
    from app.bot.keyboards.callback_data import ReviewRatingCallback
    from app.bot.states.booking_states import ReviewFSM
    from app.database.models import Review

    async with tg_harness(client_content.router) as h:
        await h.press(CLIENT_TG, ReviewsCallback(action="add"))
        await h.press(CLIENT_TG, ReviewRatingCallback(rating=5))
        await h.send_text(CLIENT_TG, "Аня")
        assert await h.get_state(CLIENT_TG) == ReviewFSM.enter_text.state

        await _restrict(session, test_user)  # мастер ограничил клиента посреди заполнения
        await h.send_text(CLIENT_TG, "Мой отзыв")
        assert h.tg.sent_to(CLIENT_TG)[-1] == RESTRICTED_REVIEWS_TEXT
        assert await h.get_state(CLIENT_TG) is None
    assert (await session.execute(select(Review))).scalars().all() == []


@pytest.mark.parametrize("action", ["open", "block"])
async def test_master_reply_resets_over_limit_counter(session, test_user, action):
    """Ответ мастера ограниченному клиенту (открыть доступ или ограничить навсегда) обнуляет счётчик «больше трёх записей»."""
    user_id = test_user.id
    await _restrict(session, test_user)
    await set_client(session, test_user, over_limit_cancels=2)
    async with tg_harness(*ROUTERS) as h:
        await _write(h)
        req = await _only_request(session)
        await h.press(ADMIN_ID, AdminAccessRequestCallback(request_id=req.id, action=action))
    assert (await client_row(session, user_id)).over_limit_cancels is None

