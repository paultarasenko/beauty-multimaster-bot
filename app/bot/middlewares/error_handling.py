import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.log_context import update_log_context

logger = logging.getLogger(__name__)

GENERIC_ERROR_TEXT = "⚠️ Что-то пошло не так. Попробуйте ещё раз или вернитесь в главное меню /start."


class ErrorHandlingMiddleware(BaseMiddleware):
    """Подключается на dp.update — оборачивает обработку целого Update, включая DatabaseSessionMiddleware.

    На этом уровне event — это объект Update, а не Message/CallbackQuery, поэтому исходное сообщение
    или колбэк достаём из его полей event.message / event.callback_query. Без этого isinstance-проверки
    никогда не совпадали: пользователь не получал GENERIC_ERROR_TEXT, а кнопка оставалась «висеть»
    без ответа (Telegram сам уберёт часы на ней только по таймауту).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot = data.get("bot")
        # Контекст логов открывается здесь (самый внешний middleware): запись об ошибке ниже тоже знает бота и мастера.
        with update_log_context(bot_id=getattr(bot, "id", None)):
            return await self._handle(handler, event, data)

    async def _handle(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Unhandled error while processing update")
            message = event.message if isinstance(event, Update) else event if isinstance(event, Message) else None
            callback = (
                event.callback_query if isinstance(event, Update) else event if isinstance(event, CallbackQuery) else None
            )
            try:
                if message is not None:
                    await message.answer(GENERIC_ERROR_TEXT)
                elif callback is not None:
                    await callback.answer(GENERIC_ERROR_TEXT, show_alert=True)
            except Exception:
                logger.exception("Failed to notify user about error")
            return None
