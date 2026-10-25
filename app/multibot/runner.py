"""Запуск мульти-бот режима (RUN_MODE=webhook): реестр ботов, общий Dispatcher, планировщики, FastAPI на uvicorn."""

import logging

import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.dispatcher import build_dispatcher
from app.bot.fsm_storage import FsmStorageError, close_fsm, describe_backend, ensure_fsm_ready
from app.config.settings import settings
from app.multibot.crypto import ConfigurationError, TokenCipher
from app.multibot.management import normalize_public_base_url
from app.multibot.redact import install_log_redaction
from app.multibot.registry import BotRegistry
from app.multibot.admission import UpdateAdmission
from app.multibot.call_guard import call_guard
from app.multibot.webhook import create_webhook_app
from app.scheduler.service import SchedulerService
from app.scheduler.tenants import RegistryTenantSource
from app.superadmin.runtime import build_runtime as build_superadmin, ensure_ready as superadmin_ready

logger = logging.getLogger(__name__)


def validate_webhook_settings() -> TokenCipher:
    """Без ключа шифрования и https-адреса webhook-режим не стартует. Возвращает шифр."""
    cipher = TokenCipher.from_settings()
    normalize_public_base_url(settings.public_base_url)
    return cipher


async def run_webhook(session_factory: async_sessionmaker) -> None:
    try:
        cipher = validate_webhook_settings()
    except ConfigurationError as exc:
        raise SystemExit(f"RUN_MODE=webhook: {exc}") from None

    install_log_redaction()
    registry = BotRegistry(session_factory, cipher)
    count = await registry.load_all()
    active = registry.active_entries()
    logger.info("Режим webhook: ботов в БД — %s, активных — %s", count, len(active))
    if not active:
        logger.warning("Активных ботов нет: добавьте бота командой `python -m app.cli add-bot`")

    # Суперадмин-бот (этап 9): None, если SUPERADMIN_BOT_TOKEN не задан — тогда всё работает как раньше.
    superadmin = build_superadmin(session_factory, registry=registry, cipher=cipher)
    alerts = superadmin.alerts if superadmin is not None else None

    dp = None
    try:
        dp = build_dispatcher(session_factory, workflow_data={"bot_registry": registry, "alerts": alerts})
        # Fail fast (этап 7): недоступный Redis — понятная ошибка и остановка до запуска планировщика и сервера.
        await ensure_fsm_ready(dp.storage)
    except FsmStorageError as exc:
        await registry.close()
        if superadmin is not None:
            await superadmin.stop()
        if dp is not None:
            await close_fsm(dp)
        raise SystemExit(f"RUN_MODE=webhook, хранилище FSM: {exc}") from None
    logger.info("Хранилище FSM: %s", describe_backend(dp.storage))
    if superadmin is not None and not await superadmin_ready(superadmin):
        superadmin, alerts = None, None  # суперадмин-бот выключен (причина в логе), остальное работает
        dp.workflow_data["alerts"] = None

    # Один планировщик на всех мастеров (этап 6): на каждом тике список ботов читается из БД заново, поэтому бот,
    # добавленный/отключённый через CLI, подхватывается без перезапуска. Стартует и останавливается вместе с приложением.
    scheduler = SchedulerService(
        session_factory,
        RegistryTenantSource(registry),
        alerts=alerts,
        digest=superadmin.digest if superadmin is not None else None,  # ежедневная сводка о сроках (этап 10)
    )

    # Этап 12C: недействительный токен, замеченный при ответе на апдейт, обрабатывается так же, как в планировщике —
    # бот помечается неактивным, суперадмину уходит один сигнал (повторы глушит AlertNotifier и охранник вызовов).
    async def on_unauthorized(telegram_bot_id: int) -> None:
        entry = await registry.get(telegram_bot_id)
        if entry is None:  # не бот мастера (например, суперадмин-бот): у него свой путь сигналов
            return
        await registry.deactivate_bot(telegram_bot_id)
        if alerts is not None:
            from app.superadmin.alerts import KIND_INVALID_TOKEN, invalid_token_text

            await alerts.notify(KIND_INVALID_TOKEN, telegram_bot_id, invalid_token_text(entry.master_id, telegram_bot_id))

    call_guard.on_unauthorized = on_unauthorized
    admission = UpdateAdmission(
        max_total=settings.webhook_max_concurrent_updates,
        max_per_bot=settings.webhook_bot_max_concurrency,
        timeout_seconds=settings.webhook_queue_timeout_seconds,
    )

    app = create_webhook_app(
        registry,
        dp,
        max_body_bytes=settings.webhook_max_body_bytes,
        scheduler=scheduler,
        session_factory=session_factory,
        superadmin=superadmin,
        admission=admission,
    )
    # access_log выключен: в строке запроса стоит секрет webhook. timeout_graceful_shutdown (этап 12C): при остановке текущие
    # запросы дообрабатываются не дольше этого срока (по умолчанию uvicorn ждёт бесконечно).
    config = uvicorn.Config(
        app,
        host=settings.webhook_host,
        port=settings.webhook_port,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=int(settings.scheduler_shutdown_timeout_seconds),
    )
    server = uvicorn.Server(config)
    try:
        if superadmin is not None:
            await superadmin.start_webhook(settings.public_base_url)
        logger.info("Webhook-сервер: порт %s", settings.webhook_port)
        await server.serve()
    finally:
        if superadmin is not None:
            await superadmin.stop()
        await scheduler.stop()  # страховка: обычно планировщик уже остановлен lifespan'ом приложения (повторный вызов безопасен)
        await close_fsm(dp)  # то же для соединений FSM-хранилища (если lifespan не дошёл до остановки)
        call_guard.on_unauthorized = None
        await registry.close()
        # Этап 12C: пул соединений БД закрывается последним — после планировщика, суперадмин-бота и реестра.
        bind = session_factory.kw.get("bind")
        if bind is not None:
            await bind.dispose()
