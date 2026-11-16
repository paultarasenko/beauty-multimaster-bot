"""Чистый чат (app/bot/screens.py): удаление нажатий кнопок и временных экранов, команды синей кнопки «Меню».

Тест без БД: Telegram подменён записью запросов, мастер — заглушкой.
"""

import datetime as dt

import pytest
from types import SimpleNamespace

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, DeleteMessages, SendMessage
from aiogram.types import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from app.bot.screen_tracking import ScreenStore, screen_tracking, untracked
from app.bot.screens import CommandAsButtonMiddleware, ScreenCleanupMiddleware

OWNER_ID = 777
CLIENT_ID = 5

KEYBOARD = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="x")]])
INLINE = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="1", callback_data="1")]])


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self._next_id = 100

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            self._next_id += 1
            return Message(
                message_id=self._next_id,
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True


class FakeTenant(BaseMiddleware):
    async def __call__(self, handler, event, data):
        data["tenant"] = SimpleNamespace(master=SimpleNamespace(owner_telegram_id=OWNER_ID), master_id=1)
        data["session"] = None
        return await handler(event, data)


def build_router() -> Router:
    router = Router()



    @router.message(F.text == "/start")
    async def start(message: Message):
        await message.answer("Привет", reply_markup=KEYBOARD)


    @router.message(F.text == "🕒 Проверить время")
    async def check_time(message: Message):
        await message.answer("Выберите дату", reply_markup=INLINE)


    @router.message(F.text == "ℹ️ Информация")
    async def info(message: Message):
        await message.answer("Выберите действие", reply_markup=KEYBOARD)


    @router.message(F.text == "📋 Мои записи")
    async def my_bookings(message: Message):
        await message.answer("Ваши записи")


    @router.message(F.text == "📅 Записи")
    async def records(message: Message):
        await message.answer("Записи мастера", reply_markup=KEYBOARD)


    @router.message(F.text == "свободный текст")
    async def free_text(message: Message):
        await message.answer("принял")


    @router.message(F.text == "notify")
    async def notify(message: Message):
        with untracked():
            await message.answer("🔔 уведомление")


    @router.message(F.text == "🕐 Расписание")
    async def failing(message: Message):
        raise RuntimeError("boom")


    @router.message(F.text == "✨ Обо мне")
    async def shows_nothing(message: Message):
        return None
    return router


class Chatter:
    def __init__(self) -> None:
        self.tg = RecordingSession()
        self.bot = Bot("123:abc", session=self.tg)
        self.tg.middleware(screen_tracking)
        storage = MemoryStorage()
        self.dp = Dispatcher(storage=storage)
        self.dp.update.middleware(FakeTenant())
        self.dp.update.middleware(ScreenCleanupMiddleware(ScreenStore.for_storage(storage)))
        self.dp.message.outer_middleware(CommandAsButtonMiddleware())
        self.dp.include_router(build_router())
        self._message_id = 0

    async def say(self, user_id: int, text: str) -> tuple[list[str], list[int]]:
        """Возвращает (тексты отправленных сообщений, id удалённых сообщений)."""
        self._message_id += 1
        message = Message(
            message_id=self._message_id,
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="p"),
            text=text,
        )
        self.tg.requests.clear()
        await self.dp.feed_update(self.bot, Update(update_id=self._message_id, message=message))
        sent = [m.text for m in self.tg.requests if isinstance(m, SendMessage)]
        deleted: list[int] = []
        for m in self.tg.requests:
            if isinstance(m, DeleteMessages):
                deleted += m.message_ids
            elif isinstance(m, DeleteMessage):
                deleted.append(m.message_id)
        return sent, deleted


async def test_button_press_message_is_deleted_and_reply_stays():
    chat = Chatter()
    sent, deleted = await chat.say(CLIENT_ID, "🕒 Проверить время")
    assert sent == ["Выберите дату"]
    assert deleted == [1]  # сообщение с названием кнопки


async def test_previous_screens_are_removed_on_next_section():
    chat = Chatter()
    await chat.say(CLIENT_ID, "/start")  # 101 — носитель клавиатуры
    await chat.say(CLIENT_ID, "🕒 Проверить время")  # 102 — календарь
    _, deleted = await chat.say(CLIENT_ID, "📋 Мои записи")  # без новой клавиатуры
    assert 102 in deleted  # календарь убран
    assert 101 not in deleted  # на нём висит клавиатура: пока нет нового носителя, не трогаем
    _, deleted = await chat.say(CLIENT_ID, "ℹ️ Информация")  # присылает новый носитель клавиатуры (104)
    assert 101 in deleted  # новый носитель уже показан — старый можно убрать


async def test_typed_text_stays_and_screen_it_triggered_is_temporary():
    chat = Chatter()
    await chat.say(CLIENT_ID, "/start")
    sent, deleted = await chat.say(CLIENT_ID, "свободный текст")
    assert sent == ["принял"] and deleted == []  # введённый руками текст не удаляется
    _, deleted = await chat.say(CLIENT_ID, "📋 Мои записи")
    assert 102 in deleted  # ответ бота на него — временный


async def test_untracked_notification_survives_navigation():
    chat = Chatter()
    await chat.say(OWNER_ID, "/start")
    await chat.say(OWNER_ID, "notify")  # 102 — уведомление, не запоминается
    _, deleted = await chat.say(OWNER_ID, "📋 Мои записи")
    assert 102 not in deleted


async def test_command_works_as_button_for_client():
    chat = Chatter()
    sent, deleted = await chat.say(CLIENT_ID, "/my")
    assert sent == ["Ваши записи"]  # как нажатие «📋 Мои записи»
    assert deleted == [1]


async def test_admin_command_only_for_owner(monkeypatch):
    import app.bot.menu_mode as menu_mode

    entered: list[int] = []

    async def fake_enter(session, master_id, telegram_id):
        entered.append(telegram_id)

    monkeypatch.setattr(menu_mode, "enter_admin_panel", fake_enter)
    chat = Chatter()
    sent, _ = await chat.say(OWNER_ID, "/records")
    assert sent[-1] == "Записи мастера" and entered == [OWNER_ID]
    entered.clear()
    sent, _ = await chat.say(CLIENT_ID, "/records")
    assert sent == [] and entered == []  # клиенту админская команда ничего не открывает


async def test_menu_commands_are_set_once_per_bot_and_owner():
    from aiogram.methods import SetMyCommands

    chat = Chatter()
    await chat.say(OWNER_ID, "/start")
    first = [m for m in chat.tg.requests if isinstance(m, SetMyCommands)]
    assert len(first) == 2  # клиентский список (всем) и админский (чат владельца)
    await chat.say(OWNER_ID, "/start")
    assert not [m for m in chat.tg.requests if isinstance(m, SetMyCommands)]


async def test_new_screen_is_sent_before_old_ones_are_deleted():
    chat = Chatter()
    await chat.say(CLIENT_ID, "/start")
    await chat.say(CLIENT_ID, "🕒 Проверить время")  # 102 — экран, который будет убран
    await chat.say(CLIENT_ID, "📋 Мои записи")
    order = [type(m) for m in chat.tg.requests if isinstance(m, (SendMessage, DeleteMessage, DeleteMessages))]
    assert order[0] is SendMessage  # сначала показано новое
    assert all(t is not SendMessage for t in order[1:])  # дальше только удаления


async def test_old_screens_stay_if_handler_fails():
    chat = Chatter()
    await chat.say(OWNER_ID, "/start")
    await chat.say(OWNER_ID, "🕒 Проверить время")  # 102
    with pytest.raises(RuntimeError):
        await chat.say(OWNER_ID, "🕐 Расписание")
    deleted = [m for m in chat.tg.requests if isinstance(m, (DeleteMessage, DeleteMessages))]
    deleted_ids = [i for m in deleted for i in (m.message_ids if isinstance(m, DeleteMessages) else [m.message_id])]
    assert 102 not in deleted_ids  # прежний экран не потерян


async def test_old_screens_stay_if_nothing_new_is_shown():
    chat = Chatter()
    await chat.say(CLIENT_ID, "/start")
    await chat.say(CLIENT_ID, "🕒 Проверить время")  # 102
    _, deleted = await chat.say(CLIENT_ID, "✨ Обо мне")
    assert 102 not in deleted
