"""Сборка Dispatcher: middleware и роутеры. Используется и в режиме polling, и в режиме webhook (этап 4)."""

from typing import Any, Iterable

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers.admin import about as admin_about
from app.bot.handlers.admin import access_requests as admin_access_requests
from app.bot.handlers.admin import add_booking as admin_add_booking
from app.bot.handlers.admin import block_time as admin_block_time
from app.bot.handlers.admin import bookings as admin_bookings
from app.bot.handlers.admin import client_access as admin_client_access
from app.bot.handlers.admin import content as admin_content
from app.bot.handlers.admin import limit_requests as admin_limit_requests
from app.bot.handlers.admin import master_settings as admin_master_settings
from app.bot.handlers.admin import my_bot as admin_my_bot
from app.bot.handlers.admin import portfolio as admin_portfolio
from app.bot.handlers.admin import reminders as admin_reminders
from app.bot.handlers.admin import reviews as admin_reviews
from app.bot.handlers.admin import schedule as admin_schedule
from app.bot.handlers.admin import special_days as admin_special_days
from app.bot.handlers.admin import services as admin_services
from app.bot.handlers.admin import stats as admin_stats
from app.bot.handlers.client import booking as client_booking
from app.bot.handlers.client import bot_blocked as client_bot_blocked
from app.bot.handlers.client import check_time as client_check_time
from app.bot.handlers.client import contact_master as client_contact_master
from app.bot.handlers.client import content as client_content
from app.bot.handlers.client import master_request as client_master_request
from app.bot.handlers.client import my_booking as client_my_booking
from app.bot.handlers.client import reminder_actions as client_reminder_actions
from app.bot.handlers.client import start as client_start
from app.bot.disabled_replies import DisabledReplyThrottle
from app.bot.fsm_storage import create_fsm_backend, isolation_for_storage
from app.bot.middlewares.db_session import DatabaseSessionMiddleware
from app.bot.middlewares.error_handling import ErrorHandlingMiddleware
from app.bot.middlewares.reset_input import ResetInputOnMenuPressMiddleware
from app.bot.middlewares.storage_guard import FsmStorageGuardMiddleware
from app.bot.middlewares.tenant import TenantMiddleware


def default_routers() -> list[Router]:
    """Роутеры бота в порядке подключения.

    Порядок важен: admin-роутеры первыми (у них есть свой IsAdminFilter, так что обычным пользователям они прозрачны),
    затем клиентские. Обращение ограниченного клиента к мастеру должно стоять раньше admin_content: тот тоже ловит
    «📞 Связаться со мной».
    """
    return [
        client_contact_master.router,
        admin_bookings.router,
        admin_add_booking.router,
        admin_schedule.router,
        admin_special_days.router,
        admin_block_time.router,
        admin_services.router,
        admin_client_access.router,
        admin_limit_requests.router,
        admin_access_requests.router,
        admin_stats.router,
        admin_content.router,
        admin_portfolio.router,
        admin_reviews.router,
        admin_about.router,
        admin_reminders.router,
        admin_master_settings.router,
        admin_my_bot.router,
        client_bot_blocked.router,
        client_master_request.router,
        client_start.router,
        client_booking.router,
        client_my_booking.router,
        client_reminder_actions.router,
        client_check_time.router,
        client_content.router,
    ]


def build_dispatcher(
    session_factory: async_sessionmaker,
    *,
    storage: BaseStorage | None = None,
    events_isolation: BaseEventIsolation | None = None,
    routers: Iterable[Router] | None = None,
    workflow_data: dict[str, Any] | None = None,
) -> Dispatcher:
    """Dispatcher с общими middleware и роутерами.

    Хранилище FSM выбирается ЗДЕСЬ (этап 7, `app/bot/fsm_storage.py`): по FSM_STORAGE, а если он не задан — по режиму
    (webhook → Redis, polling → память). Явно переданные `storage`/`events_isolation` (тесты) имеют приоритет.
    Соединение с Redis ленивое; доступность проверяет вызывающий (`ensure_fsm_ready`) при старте.

    `workflow_data` попадает в data каждого апдейта; в режиме webhook сюда кладётся `bot_registry`, по которому
    resolve_tenant определяет мастера. Роутеры модульные и подключаются к диспетчеру один раз за процесс.
    """
    if storage is None:
        backend = create_fsm_backend()
        storage, events_isolation = backend.storage, events_isolation or backend.isolation
    elif events_isolation is None:
        events_isolation = isolation_for_storage(storage)
    # session_factory нужен хендлерам, которые вызывают функции управления ботом (management.*): они открывают свои сессии (этап 9).
    # disabled_replies (этап 10): антиспам вежливых ответов отключённым ботам; Redis или память — как у выбранного хранилища FSM.
    dp = Dispatcher(
        storage=storage,
        events_isolation=events_isolation,
        **{
            "session_factory": session_factory,
            "disabled_replies": DisabledReplyThrottle.for_storage(storage),
            **(workflow_data or {}),
        },
    )

    # Ошибки хранилища FSM (недоступный Redis) должны перехватываться ДО FSMContextMiddleware, который читает состояние и
    # берёт блокировку пользователя: ErrorHandlingMiddleware (dp.update.middleware) работает глубже и их не видит.
    dp.update.outer_middleware.unregister(dp.fsm)
    dp.update.outer_middleware.register(FsmStorageGuardMiddleware())
    dp.update.outer_middleware.register(dp.fsm)

    dp.update.middleware(ErrorHandlingMiddleware())
    dp.update.middleware(DatabaseSessionMiddleware(session_factory))
    dp.update.middleware(TenantMiddleware())  # после сессии БД: кладёт data["tenant"]
    dp.message.outer_middleware(ResetInputOnMenuPressMiddleware())

    for router in default_routers() if routers is None else routers:
        dp.include_router(router)
    return dp
