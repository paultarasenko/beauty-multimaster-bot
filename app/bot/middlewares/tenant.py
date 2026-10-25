import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.bot.disabled_replies import reply_to_disabled_update
from app.log_context import ensure_log_context
from app.tenancy import TenantDisabledError, TenantNotAvailableError, get_owner_telegram_id, resolve_tenant

logger = logging.getLogger(__name__)


class TenantMiddleware(BaseMiddleware):
    """Определяет мастера апдейта и кладёт TenantContext в data["tenant"].

    Подключается ПОСЛЕ DatabaseSessionMiddleware: нужна data["session"]. Если мастер недоступен
    (удалён или отключён уже после старта), апдейт отбрасывается — обрабатывать его некому. Если мастер именно ОТКЛЮЧЁН
    (этап 10), автору апдейта коротко вежливо отвечают (не чаще раза в несколько часов); до хендлеров апдейт всё равно не доходит.
    Выставляет master_id в контексте логов (`app/log_context.py`).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            data["tenant"] = await resolve_tenant(data["session"], event, data)
        except TenantDisabledError as exc:
            master_id = exc.master_id
            logger.info("Апдейт отброшен: мастер %s отключён", master_id)
            throttle, bot, session = data.get("disabled_replies"), data.get("bot"), data["session"]
            if throttle is not None and bot is not None and isinstance(event, Update):
                await reply_to_disabled_update(bot, event, throttle, lambda: get_owner_telegram_id(session, master_id))
            return None
        except TenantNotAvailableError:
            logger.exception("Апдейт отброшен: мастер недоступен")
            return None
        # Дальше все записи лога этого апдейта (в том числе внешнего ErrorHandlingMiddleware) помечены мастером.
        with ensure_log_context(bot_id=getattr(data.get("bot"), "id", None)) as log_context:
            log_context.master_id = data["tenant"].master_id
            return await handler(event, data)
