"""Сценарий мастера целиком: настоящий Dispatcher aiogram + настоящая БД, вместо Telegram — запись запросов."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.handlers.admin import reminders as reminders_handlers
from app.bot.keyboards.admin_keyboards import BTN_REMINDERS
from app.bot.keyboards.callback_data import AdminReminderCallback
from app.bot.middlewares.db_session import DatabaseSessionMiddleware
from app.bot.middlewares.tenant import TenantMiddleware
from app.database.repositories.admin_reminder_repository import AdminReminderRepository
from app.database.session import make_session_factory
from tests.conftest import MASTER_ID, TEST_DATABASE_URL, set_owner
from tests.fsm_utils import make_backend

ADMIN_ID = 4242
MINSK = ZoneInfo("Europe/Minsk")


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=ADMIN_ID, type="private"),
                text=method.text,
            )
        return True  # EditMessageText, AnswerCallbackQuery

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, (SendMessage, EditMessageText))]

    def last_markup_texts(self) -> list[list[str]]:
        markup = [m for m in self.requests if isinstance(m, (SendMessage, EditMessageText))][-1].reply_markup
        return [[b.text for b in row] for row in markup.inline_keyboard]


class Harness:
    def __init__(self, dp: Dispatcher, bot: Bot, tg: RecordingSession):
        self.dp, self.bot, self.tg = dp, bot, tg
        self._update_id = 0

    def _message(self, text: str | None) -> Message:
        return Message(
            message_id=1,
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=ADMIN_ID, type="private"),
            from_user=User(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            text=text,
        )

    async def send_text(self, text: str) -> None:
        self._update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, message=self._message(text)))

    async def press(self, action: str, recurrence: str = "none", reminder_id: int = 0) -> None:
        self._update_id += 1
        data = AdminReminderCallback(action=action, recurrence=recurrence, reminder_id=reminder_id).pack()
        callback = CallbackQuery(
            id=str(self._update_id),
            from_user=User(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            chat_instance="ci",
            message=self._message("menu"),
            data=data,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, callback_query=callback))

    def answered_alerts(self) -> list[str]:
        return [m.text for m in self.tg.requests if isinstance(m, AnswerCallbackQuery) and m.show_alert]


@pytest_asyncio.fixture
async def harness(session):
    await set_owner(session, ADMIN_ID)
    tg = RecordingSession()
    bot = Bot(token="123456:TEST", session=tg)
    backend = make_backend()  # memory или Redis — параметр fsm_kind (tests/fsm_utils.py)
    dp = Dispatcher(storage=backend.storage, events_isolation=backend.isolation)
    db_factory = make_session_factory(TEST_DATABASE_URL)
    dp.update.middleware(DatabaseSessionMiddleware(db_factory))
    dp.update.middleware(TenantMiddleware())
    dp.include_router(reminders_handlers.router)
    yield Harness(dp, bot, tg)
    await backend.storage.close()
    await db_factory.kw["bind"].dispose()
    # Модульный роутер можно подключить к диспетчеру только один раз — отсоединяем для следующего теста.
    reminders_handlers.router._parent_router = None


def tomorrow_ddmmyy() -> str:
    return (dt.datetime.now(MINSK).date() + dt.timedelta(days=1)).strftime("%d%m%y")


async def test_menu_layout_matches_spec(harness):
    await harness.send_text(BTN_REMINDERS)

    assert harness.tg.last_markup_texts() == [
        ["Каждый год"],
        ["Раз в месяц", "Каждый день"],
        ["Один раз", "Раз в неделю"],
        ["📋 Мои напоминания"],
    ]


async def test_non_admin_cannot_use_reminders(harness, session):
    await set_owner(session, 1)
    await harness.send_text(BTN_REMINDERS)
    assert harness.tg.requests == []


async def test_full_flow_create_view_delete(harness, session):
    await harness.send_text(BTN_REMINDERS)
    await harness.press("new", "once")
    assert harness.tg.sent_texts()[-1].startswith("Введите дату.")

    await harness.send_text(tomorrow_ddmmyy())
    assert harness.tg.sent_texts()[-1].startswith("Введите время.")

    await harness.send_text("1930")
    assert harness.tg.sent_texts()[-1].startswith("Что напомнить?")

    await harness.send_text("Позвонить <поставщику>")
    created = harness.tg.sent_texts()[-1]
    assert created.startswith("✅ Напоминание создано.")
    assert "Позвонить &lt;поставщику&gt;" in created  # HTML-режим: пользовательский текст экранирован
    assert "Один раз" in created

    stored = await AdminReminderRepository(session, MASTER_ID).list_upcoming(10)
    assert [(r.text, r.recurrence) for r in stored] == [("Позвонить <поставщику>", "once")]
    reminder_id = stored[0].id

    await harness.press("list")
    assert "Ваши напоминания" in harness.tg.sent_texts()[-1]
    assert "Позвонить" in harness.tg.last_markup_texts()[0][0]

    await harness.press("open", reminder_id=reminder_id)
    assert harness.tg.last_markup_texts() == [["🗑 Удалить", "⬅️ Назад"]]

    await harness.press("delete", reminder_id=reminder_id)
    assert harness.tg.sent_texts()[-1].startswith("Удалить это напоминание?")

    await harness.press("confirm", reminder_id=reminder_id)
    assert harness.tg.sent_texts()[-1] == "✅ Напоминание удалено."
    assert await AdminReminderRepository(session, MASTER_ID).count() == 0

    await harness.press("list")
    assert harness.tg.sent_texts()[-1] == "Напоминаний пока нет."


@pytest.mark.parametrize("bad_date", ["abc", "991399", "23102"])
async def test_invalid_date_keeps_asking(harness, bad_date):
    await harness.send_text(BTN_REMINDERS)
    await harness.press("new", "daily")
    await harness.send_text(bad_date)
    assert "формате ДДММГГ" in harness.tg.sent_texts()[-1]

    await harness.send_text(tomorrow_ddmmyy())  # после ошибки сценарий продолжается
    assert harness.tg.sent_texts()[-1].startswith("Введите время.")


async def test_once_rejects_past_date_and_past_time(harness):
    await harness.send_text(BTN_REMINDERS)
    await harness.press("new", "once")

    await harness.send_text("010120")
    assert "уже прошла" in harness.tg.sent_texts()[-1]

    today = dt.datetime.now(MINSK)
    await harness.send_text(today.strftime("%d%m%y"))
    past = (today - dt.timedelta(minutes=5)).replace(second=0)
    if past.date() == today.date():  # около полуночи «5 минут назад» уже вчера — пропускаем эту проверку
        await harness.send_text(past.strftime("%H%M"))
        assert "уже прошло" in harness.tg.sent_texts()[-1]


async def test_recurring_accepts_past_start_date(harness, session):
    await harness.send_text(BTN_REMINDERS)
    await harness.press("new", "yearly")
    await harness.send_text("230986")  # пример из ТЗ: дата из прошлого
    await harness.send_text("1930")
    await harness.send_text("День рождения")

    [reminder] = await AdminReminderRepository(session, MASTER_ID).list_upcoming(10)
    local = reminder.next_fire_at.astimezone(MINSK)
    assert (local.month, local.day, local.hour, local.minute) == (9, 23, 19, 30)
    assert reminder.next_fire_at > dt.datetime.now(dt.timezone.utc)
    assert "Каждый год" in harness.tg.sent_texts()[-1]


async def test_cancel_drops_the_flow(harness, session):
    await harness.send_text(BTN_REMINDERS)
    await harness.press("new", "daily")
    await harness.press("cancel")
    assert harness.tg.sent_texts()[-1] == "Создание напоминания отменено."

    await harness.send_text(tomorrow_ddmmyy())  # без активного шага обработчик ввода не срабатывает
    assert await AdminReminderRepository(session, MASTER_ID).count() == 0


async def test_stale_buttons_are_handled(harness):
    await harness.press("open", reminder_id=999)
    await harness.press("new", "hourly")
    assert len(harness.answered_alerts()) == 2
