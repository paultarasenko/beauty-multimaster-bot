import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.bot.fsm_storage import STORAGE_ERRORS
from app.bot.middlewares.error_handling import GENERIC_ERROR_TEXT
from app.config.settings import settings
from app.log_context import update_log_context

logger = logging.getLogger(__name__)

NOTIFY_TIMEOUT_SECONDS = 5.0


def _master_id_hint(data: dict[str, Any]) -> int | None:
    """Мастер апдейта для лога. TenantMiddleware к этому моменту ещё не отработал, поэтому берём из реестра ботов (webhook)
    или из настроек (polling); в режиме webhook без реестра мастера нет."""
    registry = data.get("bot_registry")
    bot = data.get("bot")
    if registry is not None:
        return registry.master_id_for_bot(bot) if bot is not None else None
    return None if settings.run_mode == "webhook" else settings.master_id


class FsmStorageGuardMiddleware(BaseMiddleware):
    """Самый внешний слой FSM (стоит перед FSMContextMiddleware на dp.update): ловит недоступность Redis.

    Состояние и блокировка пользователя читаются в FSMContextMiddleware — раньше, чем начинает работать
    ErrorHandlingMiddleware, поэтому его ошибки до пользователя не доходили бы. Здесь: запись в лог с мастером и ботом,
    короткий общий ответ пользователю и штатное завершение (endpoint отвечает Telegram 200 сразу, без шторма повторов).
    Ошибки хендлеров (в том числе обращения к Redis из хендлера) сюда не попадают — их обрабатывает ErrorHandlingMiddleware.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except STORAGE_ERRORS as exc:
            await self._report(event, data, exc)
            return None

    async def _report(self, event: TelegramObject, data: dict[str, Any], exc: BaseException) -> None:
        bot = data.get("bot")
        with update_log_context(bot_id=getattr(bot, "id", None)) as log_context:
            log_context.master_id = _master_id_hint(data)
            # Без трейсбека: при отказе Redis каждый апдейт дал бы длинную простыню. Тип ошибки достаточен.
            logger.error("Хранилище FSM недоступно, апдейт не обработан: %s", type(exc).__name__)
            message = event.message if isinstance(event, Update) else None
            callback = event.callback_query if isinstance(event, Update) else None
            try:
                if message is not None:
                    await asyncio.wait_for(message.answer(GENERIC_ERROR_TEXT), NOTIFY_TIMEOUT_SECONDS)
                elif callback is not None:
                    await asyncio.wait_for(callback.answer(GENERIC_ERROR_TEXT, show_alert=True), NOTIFY_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("Не удалось сообщить пользователю о сбое хранилища FSM")
