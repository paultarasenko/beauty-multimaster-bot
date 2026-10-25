"""Запуск суперадмин-бота внутри основного процесса (этап 9).

- webhook-режим: бот принимает апдейты на `POST /sa/{secret}` того же FastAPI-сервера (см. app/multibot/webhook.py).
  Секрет пути и значение заголовка secret_token выводятся из токена суперадмин-бота и ключа BOT_TOKEN_ENCRYPTION_KEY (HMAC),
  в БД и .env не хранятся; при смене токена или ключа webhook перерегистрируется при старте автоматически.
- polling-режим: отдельная задача рядом с основным ботом.
SUPERADMIN_BOT_TOKEN не задан → `build_runtime` возвращает None, остальное приложение работает как раньше.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.fsm_storage import FsmStorageError, close_fsm, create_fsm_backend, describe_backend, ensure_fsm_ready
from app.bot.middlewares.error_handling import ErrorHandlingMiddleware
from app.bot.middlewares.storage_guard import FsmStorageGuardMiddleware
from app.config.settings import settings
from app.multibot import management
from app.multibot.crypto import ConfigurationError, TokenCipher
from app.multibot.redact import install_log_redaction, redact
from app.multibot.registry import BotRegistry
from app.multibot.telegram_api import telegram_session
from app.superadmin.access import SuperadminAccessMiddleware, parse_superadmin_ids
from app.superadmin.alerts import AlertNotifier
from app.superadmin.digest import TrialDigest
from app.superadmin.handlers import build_router as build_superadmin_router
from app.superadmin.health import WebhookHealthMonitor

logger = logging.getLogger(__name__)

SA_ALLOWED_UPDATES = ["message", "callback_query"]


class SuperadminRuntime:
    def __init__(
        self,
        bot: Bot,
        dp: Dispatcher,
        alerts: AlertNotifier,
        recipients: frozenset[int],
        health: WebhookHealthMonitor | None,
        path_secret: str | None,
        header_token: str | None,
        digest: TrialDigest | None = None,
    ):
        self.bot, self.dp, self.alerts, self.recipients, self.health = bot, dp, alerts, recipients, health
        self.digest = digest  # ежедневная сводка о сроках пробного периода (этап 10): её запускает общий планировщик
        self.path_secret = path_secret  # только в webhook-режиме
        self.header_token = header_token
        self._polling_task: asyncio.Task | None = None

    # ---- webhook ----

    async def start_webhook(self, public_base_url: str) -> None:
        """Регистрирует webhook суперадмин-бота у Telegram и запускает периодическую проверку ботов мастеров."""
        assert self.path_secret and self.header_token
        url = f"{management.normalize_public_base_url(public_base_url)}/sa/{self.path_secret}"
        try:
            await self.bot.set_webhook(url, secret_token=self.header_token, allowed_updates=SA_ALLOWED_UPDATES)
        except Exception as exc:
            # Остальное приложение продолжает работать; суперадмин-бот ответит, когда webhook удастся установить (перезапуск).
            logger.error("Суперадмин-бот: не удалось установить webhook (%s: %s)", type(exc).__name__, redact(str(exc), url, self.header_token))
        else:
            logger.info("Суперадмин-бот: webhook установлен, доступ у %s пользователей", len(self.recipients))
        if self.health is not None:
            self.health.start()

    # ---- polling ----

    def start_polling(self) -> None:
        self._polling_task = asyncio.create_task(self._poll(), name="superadmin-polling")

    async def _poll(self) -> None:
        try:
            await self.bot.delete_webhook()  # если раньше бот работал по webhook, getUpdates не заработает
            logger.info("Суперадмин-бот: polling запущен, доступ у %s пользователей", len(self.recipients))
            await self.dp.start_polling(self.bot, handle_signals=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Суперадмин-бот: polling остановлен из-за ошибки (%s: %s)", type(exc).__name__, redact(str(exc)))

    # ---- остановка ----

    async def stop(self) -> None:
        if self.health is not None:
            await self.health.stop()
        task, self._polling_task = self._polling_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await close_fsm(self.dp)
        await self.bot.session.close()


def build_runtime(
    session_factory: async_sessionmaker, *, registry: BotRegistry | None = None, cipher: TokenCipher | None = None
) -> SuperadminRuntime | None:
    """Собирает суперадмин-бота или возвращает None, если функция выключена/настроена неверно (причина — в логе).

    `registry`, `cipher` передаются в webhook-режиме: реестр обновляется сразу после действий суперадмина, шифр нужен для секретов."""
    token = settings.superadmin_bot_token.strip()
    if not token:
        return None
    try:
        recipients = parse_superadmin_ids(settings.superadmin_telegram_ids)
    except ConfigurationError as exc:
        logger.error("Суперадмин-бот выключен: %s", exc)
        return None
    if not recipients:
        logger.error("Суперадмин-бот выключен: SUPERADMIN_TELEGRAM_IDS пуст (некому разрешать доступ)")
        return None
    install_log_redaction()  # токен может случайно оказаться в тексте сетевой ошибки

    try:
        bot = Bot(token=token, session=telegram_session(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except Exception:
        logger.error("Суперадмин-бот выключен: SUPERADMIN_BOT_TOKEN имеет неверный формат")
        return None

    alerts = AlertNotifier(bot, recipients, settings.superadmin_alert_interval_seconds)
    monitor = None
    path_secret = header_token = None
    if cipher is not None and settings.run_mode == "webhook":
        monitor = WebhookHealthMonitor(
            session_factory, cipher, alerts, public_base_url=settings.public_base_url,
            interval_seconds=settings.superadmin_health_interval_seconds,
            auto_refresh=settings.superadmin_webhook_auto_refresh,
        )
        path_secret = cipher.header_token(f"sa-path:{token}")
        header_token = cipher.header_token(f"sa-header:{token}")

    dp = _build_dispatcher(recipients)
    dp.workflow_data.update(
        session_factory=session_factory, registry=registry, health_monitor=monitor, sa_bot_id=bot.id,
    )
    digest = TrialDigest(session_factory, bot, recipients)
    return SuperadminRuntime(bot, dp, alerts, recipients, monitor, path_secret, header_token, digest)


def _build_dispatcher(recipients: frozenset[int]) -> Dispatcher:
    backend = create_fsm_backend()
    dp = Dispatcher(storage=backend.storage, events_isolation=backend.isolation)
    # Порядок как в app/bot/dispatcher.py: ошибки хранилища FSM ловит guard ДО FSM-слоя; доступ проверяется раньше всего FSM.
    dp.update.outer_middleware.unregister(dp.fsm)
    dp.update.outer_middleware.register(SuperadminAccessMiddleware(recipients))
    dp.update.outer_middleware.register(FsmStorageGuardMiddleware())
    dp.update.outer_middleware.register(dp.fsm)
    dp.update.middleware(ErrorHandlingMiddleware())
    dp.include_router(build_superadmin_router())
    return dp


async def ensure_ready(runtime: SuperadminRuntime) -> bool:
    """Хранилище FSM суперадмина доступно? При недоступном Redis суперадмин-бот не запускается, остальное приложение работает."""
    try:
        await ensure_fsm_ready(runtime.dp.storage)
    except FsmStorageError as exc:
        logger.error("Суперадмин-бот выключен: хранилище FSM недоступно (%s)", exc)
        await runtime.stop()
        return False
    logger.info("Суперадмин-бот: хранилище FSM — %s", describe_backend(runtime.dp.storage))
    return True
