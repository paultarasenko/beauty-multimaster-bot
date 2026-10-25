import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from app.bot.dispatcher import build_dispatcher
from app.bot.fsm_storage import FsmStorageError, close_fsm, describe_backend, ensure_fsm_ready
from app.config.settings import settings
from app.database.session import async_session_factory
from app.log_context import configure_logging
from app.multibot.telegram_api import telegram_session
from app.scheduler.service import SchedulerService
from app.scheduler.tenants import SingleTenantSource
from app.superadmin.runtime import build_runtime as build_superadmin, ensure_ready as superadmin_ready
from app.tenancy import bootstrap_owner, ensure_tenant_available

# Формат логов содержит master_id и bot_id апдейта (app/log_context.py): по записи видно, какого мастера она касается.
configure_logging(logging.INFO)
logger = logging.getLogger(__name__)


async def run_polling() -> None:
    # Мастер из настроек должен существовать и быть активным — иначе падаем при старте, а не на первом апдейте.
    tenant = await ensure_tenant_available(async_session_factory)
    logger.info("Мастер: id=%s", tenant.master_id)
    # ADMIN_TELEGRAM_ID — только начальная загрузка владельца: права дальше определяет masters.owner_telegram_id.
    await bootstrap_owner(async_session_factory)

    bot = Bot(token=settings.bot_token, session=telegram_session(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Суперадмин-бот (этап 9): отдельная задача рядом с основным ботом; None, если SUPERADMIN_BOT_TOKEN не задан.
    superadmin = build_superadmin(async_session_factory)
    dp = None
    try:
        dp = build_dispatcher(
            async_session_factory, workflow_data={"alerts": superadmin.alerts if superadmin is not None else None}
        )
        await ensure_fsm_ready(dp.storage)  # memory — всегда готово; FSM_STORAGE=redis — fail fast при недоступном Redis
    except FsmStorageError as exc:
        await bot.session.close()
        if superadmin is not None:
            await superadmin.stop()
        if dp is not None:
            await close_fsm(dp)
        raise SystemExit(f"Хранилище FSM: {exc}") from None
    logger.info("Хранилище FSM: %s", describe_backend(dp.storage))
    if superadmin is not None and not await superadmin_ready(superadmin):
        superadmin = None  # выключен (причина в логе), основной бот работает как раньше
        dp.workflow_data["alerts"] = None

    scheduler = SchedulerService(
        async_session_factory,
        SingleTenantSource(async_session_factory, bot, tenant.master_id),
        alerts=superadmin.alerts if superadmin is not None else None,
        digest=superadmin.digest if superadmin is not None else None,
    )
    scheduler.start()
    if superadmin is not None:
        superadmin.start_polling()

    try:
        logger.info("Starting bot polling")
        await dp.start_polling(bot)
    finally:
        await scheduler.stop()  # дожидаемся текущих отправок, затем закрываем бота
        if superadmin is not None:
            await superadmin.stop()
        await bot.session.close()


async def main() -> None:
    if settings.run_mode == "webhook":
        from app.multibot.runner import run_webhook

        await run_webhook(async_session_factory)
    else:
        await run_polling()


if __name__ == "__main__":
    asyncio.run(main())
