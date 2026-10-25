"""Сквозные сценарии двух мастеров через ОДИН общий Dispatcher и webhook (этап 5).

Telegram замокан (`tests/webhook_utils.FakeTelegram`), БД настоящая. Один и тот же человек пишет обоим ботам.
"""

import datetime as dt

import httpx
import pytest
import pytest_asyncio
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, SendMessage
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.bot.dispatcher import build_dispatcher, default_routers
from app.bot.keyboards.callback_data import (
    AdminClientAccessCallback,
    AdminServiceCallback,
    BookingActionCallback,
    ServiceCallback,
)
from app.bot.states.booking_states import AdminServiceFSM, BookingFSM
from app.database.models import Booking, BookingStatus, MasterClient, Service, User, WorkingHours
from app.database.session import make_session_factory
from app.multibot.crypto import TokenCipher
from app.multibot.registry import BotRegistry
from app.multibot.webhook import create_webhook_app
from app.services.booking_service import BookingService
from app.utils.invite import INVITE_START_PREFIX
from tests.conftest import RIGA_TZ, TEST_DATABASE_URL, client_row, make_client, next_weekday, set_owner
from tests.fsm_utils import make_backend
from tests.test_multibot_registry import add_master, add_row
from tests.test_multibot_webhook import A, B, BOT_A, BOT_B, CLIENT, OWNER_A, OWNER_B, Env
from tests.webhook_utils import FakeTelegram

BOOK_BTN = "📅 Записаться"


class TwoBots(Env):
    """Env из test_multibot_webhook + доступ к хранилищу FSM и вспомогательные действия."""

    dp = None

    async def press(self, bot: int, user_id: int, data) -> httpx.Response:
        self._uid += 1
        row = self.rows[bot]
        payload = {
            "update_id": self._uid,
            "callback_query": {
                "id": str(self._uid),
                "from": {"id": user_id, "is_bot": False, "first_name": f"user{user_id}", "username": f"user{user_id}"},
                "chat_instance": "ci",
                "message": {
                    "message_id": self._uid,
                    "date": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                    "chat": {"id": user_id, "type": "private"},
                    "text": "menu",
                },
                "data": data if isinstance(data, str) else data.pack(),
            },
        }
        return await self.post(bot, row.webhook_secret, payload)

    def key(self, bot: int, user_id: int) -> StorageKey:
        return StorageKey(bot_id=bot, chat_id=user_id, user_id=user_id)

    async def state(self, bot: int, user_id: int) -> str | None:
        return await self.dp.storage.get_state(self.key(bot, user_id))

    async def fsm_data(self, bot: int, user_id: int) -> dict:
        return await self.dp.storage.get_data(self.key(bot, user_id))

    def alerts(self, bot: int) -> list[str]:
        return [m.text for m in self.tg.methods_of(bot, AnswerCallbackQuery) if m.show_alert]


@pytest_asyncio.fixture
async def bots(session):
    cipher = TokenCipher(Fernet.generate_key().decode())
    await add_master(session, B, OWNER_B)
    await set_owner(session, OWNER_A, A)
    rows = {BOT_A: await add_row(session, cipher, A, BOT_A), BOT_B: await add_row(session, cipher, B, BOT_B)}
    tg = FakeTelegram()
    factory = make_session_factory(TEST_DATABASE_URL)
    registry = BotRegistry(factory, cipher, http_session_factory=lambda: tg)
    await registry.load_all()
    backend = make_backend()  # memory или настоящий Redis — параметр fsm_kind (tests/fsm_utils.py)
    dp = build_dispatcher(
        factory, storage=backend.storage, events_isolation=backend.isolation, workflow_data={"bot_registry": registry}
    )
    app = create_webhook_app(registry, dp, max_body_bytes=100_000)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    env = TwoBots(cipher, tg, registry, client, rows)
    env.dp = dp
    try:
        yield env
    finally:
        await client.aclose()
        await registry.close()
        await backend.storage.close()
        await factory.kw["bind"].dispose()
        for router in default_routers():
            router._parent_router = None


async def _setup_masters(session) -> dict:
    """Рабочие часы на всю неделю и по услуге у каждого мастера."""
    ids = {}
    for master in (A, B):
        for weekday in range(7):
            session.add(WorkingHours(master_id=master, weekday=weekday, is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
        service = Service(master_id=master, name=f"Услуга-{master}", price=40, duration_minutes=60, active=True, sort_order=0)
        session.add(service)
        await session.flush()
        ids[master] = service.id
    await session.commit()
    return ids


# ---------- FSM: одновременные сценарии одного человека у двух мастеров ----------


async def test_client_booking_scenarios_in_two_bots_do_not_mix(bots, session):
    service = await _setup_masters(session)

    await bots.send(BOT_A, CLIENT, BOOK_BTN)
    await bots.send(BOT_B, CLIENT, BOOK_BTN)
    assert await bots.state(BOT_A, CLIENT) == BookingFSM.select_service.state
    assert await bots.state(BOT_B, CLIENT) == BookingFSM.select_service.state

    await bots.press(BOT_A, CLIENT, ServiceCallback(service_id=service[A]))
    assert await bots.state(BOT_A, CLIENT) == BookingFSM.select_date.state
    assert (await bots.fsm_data(BOT_A, CLIENT))["service_id"] == service[A]
    # у бота B состояние человека не сдвинулось и данных услуги нет
    assert await bots.state(BOT_B, CLIENT) == BookingFSM.select_service.state
    assert "service_id" not in await bots.fsm_data(BOT_B, CLIENT)

    # id услуги мастера A, пришедший в бот B (подделанная кнопка), не принимается
    await bots.press(BOT_B, CLIENT, ServiceCallback(service_id=service[A]))
    assert bots.alerts(BOT_B) and bots.alerts(BOT_A) == []
    assert await bots.state(BOT_B, CLIENT) == BookingFSM.select_service.state
    assert "service_id" not in await bots.fsm_data(BOT_B, CLIENT)

    await bots.press(BOT_B, CLIENT, ServiceCallback(service_id=service[B]))
    assert (await bots.fsm_data(BOT_B, CLIENT))["service_id"] == service[B]
    assert (await bots.fsm_data(BOT_A, CLIENT))["service_id"] == service[A]  # сценарий A остался прежним


async def test_owner_admin_scenario_in_bot_a_does_not_leak_into_bot_b(bots, session):
    """Мастер A — владелец в боте A и обычный клиент в боте B: ввод в боте B не попадает в сценарий бота A."""
    service = await _setup_masters(session)
    await bots.press(BOT_A, OWNER_A, AdminServiceCallback(service_id=service[A], action="rename"))
    assert await bots.state(BOT_A, OWNER_A) == AdminServiceFSM.edit_name.state
    assert await bots.state(BOT_B, OWNER_A) is None

    await bots.send(BOT_B, OWNER_A, "Хакерское название")
    assert await _service_name(session, service[A]) == "Услуга-1"
    assert await _service_name(session, service[B]) == "Услуга-2"
    assert await bots.state(BOT_A, OWNER_A) == AdminServiceFSM.edit_name.state  # ввод в боте B сценарий A не завершил

    await bots.send(BOT_A, OWNER_A, "Новое имя")
    assert await _service_name(session, service[A]) == "Новое имя"
    assert await _service_name(session, service[B]) == "Услуга-2"


# Значения читаем столбцами, а не ORM-объектами: после изменений из другой сессии объекты в async-сессии «протухают».
async def _service_name(session, service_id: int) -> str:
    return (await session.execute(select(Service.name).where(Service.id == service_id))).scalar_one()


async def _booking_owner_and_token(session, booking_id: int) -> tuple:
    row = (await session.execute(select(Booking.user_id, Booking.invite_token).where(Booking.id == booking_id))).one()
    return tuple(row)


# ---------- приглашения и deep links ----------


async def _manual_booking_with_invite(session, token: str):
    day = next_weekday(1)
    session.add(WorkingHours(master_id=A, weekday=day.weekday(), is_working=True, start_time=dt.time(10), end_time=dt.time(19)))
    service = Service(master_id=A, name="Услуга-А", price=40, duration_minutes=60, active=True, sort_order=0)
    session.add(service)
    owner = await make_client(session, OWNER_A, A, first_name="Owner")
    await session.flush()
    booking = await BookingService(session, RIGA_TZ, master_id=A).create_booking(
        owner.id, service.id, dt.datetime.combine(day, dt.time(12), tzinfo=RIGA_TZ),
        manual_client_name="Анна", manual_client_contact="+375291234567",
    )
    booking.invite_token = token
    await session.commit()
    return booking.id, owner.id


async def test_invite_of_master_a_does_not_work_in_bot_b_and_binds_client_to_bot_master(bots, session):
    booking_id, owner_user_id = await _manual_booking_with_invite(session, "tokenA123456")
    link = f"/start {INVITE_START_PREFIX}tokenA123456"

    await bots.send(BOT_B, CLIENT, link)
    assert await _booking_owner_and_token(session, booking_id) == (owner_user_id, "tokenA123456")  # запись A не привязана, токен жив
    assert any("недействительна" in t for t in bots.tg.texts_of(BOT_B))
    assert not any("открыл приглашение" in t for t in bots.tg.sent_to(OWNER_B))  # владельцу B уведомление не шлётся
    masters = (await session.execute(select(MasterClient.master_id).join(User, User.id == MasterClient.user_id).where(User.telegram_id == CLIENT))).scalars().all()
    assert masters == [B]  # клиент привязан к мастеру бота, в который пришёл, и ни к кому больше

    await bots.send(BOT_A, CLIENT, link)  # тот же токен в «своём» боте работает
    client_user_id = (await session.execute(select(User.id).where(User.telegram_id == CLIENT))).scalar_one()
    assert await _booking_owner_and_token(session, booking_id) == (client_user_id, None)
    assert any("открыл приглашение" in t for t in bots.tg.texts_of(BOT_A))


async def test_foreign_and_unknown_invite_give_identical_answers(bots, session):
    await _manual_booking_with_invite(session, "tokenA123456")
    await bots.send(BOT_B, CLIENT, f"/start {INVITE_START_PREFIX}tokenA123456")
    await bots.send(BOT_B, CLIENT + 1, f"/start {INVITE_START_PREFIX}neverexisted")
    invalid = [t for t in bots.tg.texts_of(BOT_B) if "недействительна" in t]
    assert len(invalid) == 2 and invalid[0] == invalid[1]


async def test_other_start_parameters_are_just_a_plain_start(bots, session):
    await bots.send(BOT_A, CLIENT, "/start promo123")
    assert bots.tg.texts_of(BOT_A) and not any("недействительна" in t for t in bots.tg.texts_of(BOT_A))
    links = (await session.execute(select(MasterClient.master_id))).scalars().all()
    assert links == [A]


# ---------- два мастера: права, данные, независимые ограничения ----------


async def test_owner_a_restricts_client_only_at_a_and_has_no_rights_in_bot_b(bots, session):
    await _setup_masters(session)
    client = await make_client(session, CLIENT, A, first_name="Ivan")
    await make_client(session, CLIENT, B)
    await session.commit()
    client_id = client.id

    await bots.press(BOT_B, OWNER_A, AdminClientAccessCallback(user_id=client_id, action="restrict"))  # в боте B он не владелец
    assert (await client_row(session, client_id, A)).is_restricted is False
    assert (await client_row(session, client_id, B)).is_restricted is False
    assert bots.tg.per_bot.get(BOT_B, []) == []  # бот B не отреагировал вовсе

    await bots.press(BOT_A, OWNER_A, AdminClientAccessCallback(user_id=client_id, action="restrict"))
    assert (await client_row(session, client_id, A)).is_restricted is True
    assert (await client_row(session, client_id, B)).is_restricted is False  # у мастера B человек свободен


async def test_admin_entry_exists_only_in_own_bot(bots, session):
    await bots.send(BOT_A, OWNER_A, "/admin")
    await bots.send(BOT_B, OWNER_A, "/admin")
    assert any("Админ-панель" in t for t in bots.tg.texts_of(BOT_A))
    assert not any("Админ-панель" in t for t in bots.tg.texts_of(BOT_B))


async def test_forged_object_ids_through_webhook_touch_nothing(bots, session):
    service = await _setup_masters(session)
    client = await make_client(session, CLIENT, A)
    await session.commit()
    day = next_weekday(2)
    booking = await BookingService(session, RIGA_TZ, master_id=A).create_booking(
        client.id, service[A], dt.datetime.combine(day, dt.time(12), tzinfo=RIGA_TZ)
    )
    await session.commit()
    booking_id = booking.id

    # владелец A во «втором» боте и владелец B в «чужом» боте с id объектов другого мастера
    await bots.press(BOT_A, OWNER_A, AdminServiceCallback(service_id=service[B], action="delete_yes"))
    await bots.press(BOT_B, OWNER_B, AdminServiceCallback(service_id=service[A], action="toggle"))
    # клиент A пытается отменить свою запись через бота B
    await bots.press(BOT_B, CLIENT, BookingActionCallback(booking_id=booking_id, action="cancel_confirm"))

    session.expire_all()
    assert (await session.get(Service, service[B])) is not None
    assert (await session.get(Service, service[A])).active is True
    assert (await session.get(Booking, booking_id)).status == BookingStatus.CONFIRMED.value
    assert len(bots.alerts(BOT_A)) == 1 and len(bots.alerts(BOT_B)) == 2
    assert any("Запись не найдена" in a for a in bots.alerts(BOT_B))


async def test_booking_of_a_is_invisible_and_unusable_in_bot_b_for_the_same_person(bots, session):
    service = await _setup_masters(session)
    client = await make_client(session, CLIENT, A)
    await make_client(session, CLIENT, B)
    await session.commit()
    await BookingService(session, RIGA_TZ, master_id=A).create_booking(
        client.id, service[A], dt.datetime.combine(next_weekday(2), dt.time(12), tzinfo=RIGA_TZ)
    )
    await session.commit()
    await bots.send(BOT_A, CLIENT, "📋 Мои записи")
    await bots.send(BOT_B, CLIENT, "📋 Мои записи")
    assert any("Услуга-1" in t for t in bots.tg.texts_of(BOT_A))
    assert not any("Услуга-1" in t for t in bots.tg.texts_of(BOT_B))
