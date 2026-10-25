"""HTTP-сессия для Bot API (этап 12B): единая точка, через которую создаются все Bot приложения.

По умолчанию (TELEGRAM_API_BASE пусто) — настоящий https://api.telegram.org, поведение прежнее. Если адрес задан (только
нагрузочный стенд, tools/loadtest), запросы идут на него: токены уходят туда же, поэтому в логе остаётся предупреждение.
"""

import logging

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from app.config.settings import settings
from app.multibot.call_guard import call_guard

logger = logging.getLogger(__name__)


def telegram_session() -> AiohttpSession:
    """Новая HTTP-сессия aiogram: с адресом из TELEGRAM_API_BASE или стандартная.

    Этап 12C: общий таймаут запроса из настроек (у aiogram по умолчанию 60 с) и охранник вызовов (429 / недействительный токен).
    """
    timeout = settings.telegram_request_timeout_seconds
    base = settings.telegram_api_base
    if not base:
        session = AiohttpSession(timeout=timeout)
    else:
        logger.warning("TELEGRAM_API_BASE задан: запросы к Bot API идут на %s, а не в Telegram (допустимо только в нагрузочном стенде)", base)
        session = AiohttpSession(api=TelegramAPIServer.from_base(base), timeout=timeout)
    session.middleware(call_guard)
    return session
