"""Общий стенд для тестов хендлеров: настоящий Dispatcher aiogram и настоящая БД, вместо Telegram — запись запросов.

Использование (роутеры модульные, подключаются к диспетчеру один раз, поэтому — через контекстный менеджер):

    async with tg_harness(client_my_booking.router) as h:
        await h.press(user_id, BookingActionCallback(booking_id=1, action="cancel_confirm"))
        assert h.alerts() == [...]
"""

import datetime as dt
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.base import BaseStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetChat, SendMessage
from aiogram.types import CallbackQuery, Chat, ChatFullInfo, Message, Update, User

from app.bot.middlewares.db_session import DatabaseSessionMiddleware
from app.bot.middlewares.tenant import TenantMiddleware
from app.database.session import make_session_factory
from tests.conftest import TEST_DATABASE_URL
from tests.fsm_utils import make_backend

BOT_ID = 123456


class RecordingSession(BaseSession):
    """Подменяет сетевой слой Bot: ничего не отправляет, всё запоминает."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self.blocked_chat_ids: set[int] = set()  # кто «заблокировал бота»: отправка туда падает как в Telegram

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            if method.chat_id in self.blocked_chat_ids:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
            return Message(
                message_id=len(self.requests),
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, GetChat):
            return ChatFullInfo(
                id=method.chat_id, type="private", accent_color_id=0, max_reaction_count=0, username="master_test"
            )
        return True  # EditMessageText, AnswerCallbackQuery, DeleteMessage ...

    def texts(self) -> list[str]:
        """Тексты всех отправленных и отредактированных сообщений по порядку."""
        return [m.text for m in self.requests if isinstance(m, (SendMessage, EditMessageText))]

    def sent_to(self, chat_id: int) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, SendMessage) and m.chat_id == chat_id]

    def everything_shown(self) -> list[str]:
        """Всё, что увидит пользователь: сообщения, правки и ответы на нажатия (всплывающие уведомления тоже)."""
        answers = [m.text for m in self.requests if isinstance(m, AnswerCallbackQuery) and m.text]
        return self.texts() + answers

    def alerts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, AnswerCallbackQuery) and m.show_alert]

    def edited(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, EditMessageText)]


class Harness:
    def __init__(self, dp: Dispatcher, bot: Bot, tg: RecordingSession, storage: BaseStorage):
        self.dp, self.bot, self.tg, self.storage = dp, bot, tg, storage
        self._update_id = 0

    @staticmethod
    def _user(user_id: int) -> User:
        return User(id=user_id, is_bot=False, first_name=f"user{user_id}", username=f"user{user_id}")

    def _message(self, user_id: int, text: str | None) -> Message:
        return Message(
            message_id=1,
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=self._user(user_id),
            text=text,
        )

    async def send_text(self, user_id: int, text: str) -> None:
        self._update_id += 1
        update = Update(update_id=self._update_id, message=self._message(user_id, text))
        await self.dp.feed_update(self.bot, update)

    async def press(self, user_id: int, data: CallbackData | str) -> None:
        self._update_id += 1
        packed = data if isinstance(data, str) else data.pack()
        callback = CallbackQuery(
            id=str(self._update_id),
            from_user=self._user(user_id),
            chat_instance="ci",
            message=self._message(user_id, "menu"),
            data=packed,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, callback_query=callback))

    def fsm(self, user_id: int) -> FSMContext:
        return FSMContext(self.storage, StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id))

    async def set_state(self, user_id: int, state: State | None, **data) -> None:
        ctx = self.fsm(user_id)
        await ctx.set_state(state)
        await ctx.update_data(**data)

    async def get_state(self, user_id: int) -> str | None:
        return await self.fsm(user_id).get_state()


@asynccontextmanager
async def tg_harness(*routers, error_middleware: bool = False, reset_input: bool = False, master_id: int = 1):
    from app.bot.middlewares.error_handling import ErrorHandlingMiddleware
    from app.bot.middlewares.reset_input import ResetInputOnMenuPressMiddleware

    from app.config.settings import settings

    previous_master_id, settings.master_id = settings.master_id, master_id
    tg = RecordingSession()
    bot = Bot(token=f"{BOT_ID}:TEST", session=tg)
    backend = make_backend()  # memory или настоящий Redis — по параметру fsm_kind теста (tests/fsm_utils.py)
    storage = backend.storage
    dp = Dispatcher(storage=storage, events_isolation=backend.isolation)
    if error_middleware:
        dp.update.middleware(ErrorHandlingMiddleware())
    db_factory = make_session_factory(TEST_DATABASE_URL)
    dp.update.middleware(DatabaseSessionMiddleware(db_factory))
    dp.update.middleware(TenantMiddleware())  # мастер — из settings.master_id (тест может подменить через monkeypatch)
    if reset_input:
        dp.message.outer_middleware(ResetInputOnMenuPressMiddleware())  # как в app/main.py
    for router in routers:
        dp.include_router(router)
    try:
        yield Harness(dp, bot, tg, storage)
    finally:
        # Модульный роутер можно подключить к диспетчеру только один раз — отсоединяем для следующего теста.
        for router in routers:
            router._parent_router = None
        settings.master_id = previous_master_id
        await storage.close()
        await db_factory.kw["bind"].dispose()
