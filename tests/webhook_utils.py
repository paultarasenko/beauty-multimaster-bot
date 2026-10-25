"""Общие средства тестов мульти-бот режима: фейковый Telegram, ключ шифрования, сборка webhook-приложения."""

import datetime as dt

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe, SendMessage, SetWebhook
from aiogram.types import User

from tests.tg import RecordingSession

TOKEN_TAIL = "A" * 35


def token_for(bot_id: int) -> str:
    return f"{bot_id}:{TOKEN_TAIL}"


class FakeTelegram(RecordingSession):
    """Общая «HTTP-сессия» Bot'ов: Telegram не вызывается, запросы запоминаются по id бота."""

    def __init__(self) -> None:
        super().__init__()
        self.per_bot: dict[int, list] = {}
        self.closed = False
        self.fail_set_webhook = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.per_bot.setdefault(bot.id, []).append(method)
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="bot", username=f"bot{bot.id}_bot")
        if isinstance(method, SetWebhook) and self.fail_set_webhook:
            # Как у настоящего aiohttp/Telegram: текст ошибки может содержать токен — CLI обязан его замаскировать.
            raise TelegramBadRequest(method=method, message=f"Bad Request: failed for {bot.token}")
        return await super().make_request(bot, method, timeout)

    def texts_of(self, bot_id: int) -> list[str]:
        return [m.text for m in self.per_bot.get(bot_id, []) if isinstance(m, SendMessage)]

    def methods_of(self, bot_id: int, cls) -> list:
        return [m for m in self.per_bot.get(bot_id, []) if isinstance(m, cls)]


def message_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(dt.datetime.now(dt.timezone.utc).timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": f"user{user_id}", "username": f"user{user_id}"},
            "text": text,
        },
    }
