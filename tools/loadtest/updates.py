"""Апдейты Telegram для нагрузки: сборка JSON и разбор inline-клавиатур настоящими классами CallbackData приложения.

Нагрузка не придумывает callback_data сама: кнопки берутся из ответа бота (поддельный Telegram запоминает последнюю клавиатуру),
разбираются `CallbackData.unpack` приложения и отправляются обратно в том же виде. Так цепочка записи ломается в тесте, если
меняется формат кнопок или тексты меню.
"""

import random
import time
from collections.abc import Callable

from aiogram.filters.callback_data import CallbackData

from app.bot.keyboards.callback_data import (  # noqa: F401  (реэкспорт для сценариев)
    BookingActionCallback,
    ConfirmCallback,
    DateCallback,
    ServiceCallback,
    TimeCallback,
)

# Тексты кнопок главного меню клиента (app/bot/keyboards/client_keyboards.py: main_menu_keyboard); тест сверяет их с клавиатурой.
BOOK_TEXT = "📅 Записаться"
MY_BOOKINGS_TEXT = "📋 Мои записи"
MENU_TEXTS = ["💰 Услуги и цены", "📸 Портфолио", "ℹ️ Информация", MY_BOOKINGS_TEXT, BOOK_TEXT]


def telegram_user(telegram_id: int) -> dict:
    return {"id": telegram_id, "is_bot": False, "first_name": f"Клиент {telegram_id}", "username": f"lt_{telegram_id}", "language_code": "ru"}


def _chat_message(telegram_id: int, message_id: int, text: str | None = None, *, command: bool = False) -> dict:
    message: dict = {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": telegram_id, "type": "private", "first_name": f"Клиент {telegram_id}"},
        "from": telegram_user(telegram_id),
    }
    if text is not None:
        message["text"] = text
        if command:
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return message


def message_update(update_id: int, telegram_id: int, text: str, message_id: int) -> dict:
    """Текстовое сообщение клиента; «/…» отправляется как команда (с entities)."""
    return {"update_id": update_id, "message": _chat_message(telegram_id, message_id, text, command=text.startswith("/"))}


def callback_update(update_id: int, telegram_id: int, data: str, message_id: int, callback_id: str) -> dict:
    """Нажатие inline-кнопки под сообщением бота с id `message_id`."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": telegram_user(telegram_id),
            "chat_instance": f"ci{telegram_id}",
            "message": _chat_message(telegram_id, message_id, "·"),
            "data": data,
        },
    }


def find_callbacks(markup: dict | None, cls: type[CallbackData]) -> list[CallbackData]:
    """Все кнопки клавиатуры, чей callback_data разбирается классом `cls` (прочие кнопки пропускаются)."""
    found: list[CallbackData] = []
    for row in (markup or {}).get("inline_keyboard", []):
        for button in row:
            data = button.get("callback_data")
            if not data or not data.startswith(f"{cls.__prefix__}{cls.__separator__}"):
                continue
            try:
                found.append(cls.unpack(data))
            except Exception:  # чужой формат — не наша кнопка
                continue
    return found


def pick_callback(
    markup: dict | None, cls: type[CallbackData], rng: random.Random, predicate: Callable[[CallbackData], bool] | None = None
) -> CallbackData | None:
    options = [cb for cb in find_callbacks(markup, cls) if predicate is None or predicate(cb)]
    return rng.choice(options) if options else None
