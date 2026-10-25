"""Доступ к суперадмин-боту: только Telegram id из SUPERADMIN_TELEGRAM_IDS, остальным — молчаливый отказ."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.multibot.crypto import ConfigurationError


def parse_superadmin_ids(raw: str) -> frozenset[int]:
    """«123, 456» → {123, 456}. Пустые элементы пропускаются; нечисловое значение — ошибка конфигурации (а не молчаливый пропуск)."""
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            raise ConfigurationError("SUPERADMIN_TELEGRAM_IDS: ожидаются числовые Telegram id через запятую") from None
        if value <= 0:
            raise ConfigurationError("SUPERADMIN_TELEGRAM_IDS: Telegram id должен быть положительным числом")
        ids.add(value)
    return frozenset(ids)


class SuperadminAccessMiddleware(BaseMiddleware):
    """Outer-middleware на dp.update: апдейт не от суперадмина отбрасывается без какого-либо ответа.

    Стоит раньше FSM-слоя, поэтому для посторонних не читается ни состояние, ни блокировка пользователя.
    """

    def __init__(self, allowed_ids: frozenset[int]):
        self._allowed = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")  # кладёт UserContextMiddleware (он зарегистрирован в Dispatcher раньше)
        if user is None or user.id not in self._allowed:
            return None
        return await handler(event, data)
