"""Webhook-сервер (FastAPI): Telegram → POST /tg/{telegram_bot_id}/{secret} → реестр → общий Dispatcher.

Апдейт обрабатывается в запросе (ответ 200 после обработки): так при падении процесса посреди обработки Telegram
повторит доставку, а ошибки самой обработки логируются и не приводят к повторам (всегда 200). Любые проблемы с телом уже
аутентифицированного запроса тоже отвечают 200: одно «битое» сообщение иначе блокировало бы очередь доставки бота.
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from aiogram import Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.disabled_replies import reply_to_disabled_update
from app.bot.fsm_storage import close_fsm, ensure_fsm_ready
from app.multibot.admission import AdmissionTimeout, UpdateAdmission
from app.multibot.registry import BotEntry, BotRegistry
from app.scheduler.service import SchedulerService
from app.tenancy import get_owner_telegram_id

if TYPE_CHECKING:
    from app.superadmin.runtime import SuperadminRuntime

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
READY_CHECK_TIMEOUT_SECONDS = 3.0


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def _read_body(request: Request, limit: int) -> bytes | None:
    """Тело запроса, но не больше `limit` байт; больше — None (отвечаем 413, лишнее не читаем)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


async def _check_db(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        await session.execute(text("SELECT 1"))


async def _politely_decline(
    entry: BotEntry, body: bytes, dp: Dispatcher, session_factory: async_sessionmaker | None
) -> None:
    """Мастер отключён, а бот активен (этап 10): автору апдейта коротко вежливо отвечаем (антиспам — в disabled_replies).

    Сам апдейт до хендлеров не доходит. Любые сбои — в лог, ответ Telegram по-прежнему 200."""
    throttle = dp.workflow_data.get("disabled_replies")
    if throttle is None or session_factory is None or entry.bot is None:
        return
    try:
        update = Update.model_validate_json(body, context={"bot": entry.bot})
    except (ValidationError, ValueError):
        return

    async def owner() -> int | None:
        async with session_factory() as session:
            return await get_owner_telegram_id(session, entry.master_id)

    await reply_to_disabled_update(entry.bot, update, throttle, owner)


def create_webhook_app(
    registry: BotRegistry,
    dp: Dispatcher,
    *,
    max_body_bytes: int,
    scheduler: SchedulerService | None = None,
    session_factory: async_sessionmaker | None = None,
    superadmin: "SuperadminRuntime | None" = None,
    admission: UpdateAdmission | None = None,
) -> FastAPI:
    """`scheduler` (этап 6) стартует и останавливается вместе с приложением (lifespan): при остановке текущие отправки
    доводятся до конца раньше, чем runner закроет реестр ботов. Потом закрываются соединения хранилища FSM (этап 7).

    `session_factory` нужен для GET /ready (проверка БД); без него /ready проверяет только хранилище FSM.

    `superadmin` (этап 9) — суперадмин-бот: его апдейты приходят на отдельный путь POST /sa/{secret}; без него пути нет (404).

    `admission` (этап 12C) — ограничение параллелизма апдейтов (общее и на бота) до открытия сессии БД; не дождались места —
    503 (Telegram повторит доставку). Без него ограничений нет (как раньше)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            await close_fsm(dp)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        # Liveness: процесс жив и отвечает. Зависимости (БД, Redis) не проверяются — это делает /ready.
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness: БД отвечает и, если FSM в Redis, отвечает Redis. Подробности ошибок наружу не отдаются."""

        async def check_db() -> str:
            if session_factory is None:
                return "skipped"
            await asyncio.wait_for(_check_db(session_factory), READY_CHECK_TIMEOUT_SECONDS)
            return "ok"

        async def check_redis() -> str:
            if not isinstance(dp.storage, RedisStorage):
                return "skipped"
            await ensure_fsm_ready(dp.storage)
            return "ok"

        results = {}
        for name, check in (("db", check_db), ("redis", check_redis)):
            try:
                results[name] = await check()
            except Exception as exc:
                logger.warning("/ready: проверка %s не пройдена (%s)", name, type(exc).__name__)
                results[name] = "fail"
        ok = "fail" not in results.values()
        return JSONResponse({"status": "ready" if ok else "not_ready", **results}, status_code=200 if ok else 503)

    @app.post("/tg/{bot_id}/{secret}")
    async def receive(bot_id: str, secret: str, request: Request) -> Response:
        # Неизвестный бот и неверный секрет пути неотличимы снаружи (404 без тела).
        if not bot_id.isdigit():
            return Response(status_code=404)
        entry = await registry.get(int(bot_id))
        if entry is None or not _equal(secret, entry.webhook_secret):
            return Response(status_code=404)
        if not _equal(request.headers.get(SECRET_HEADER, ""), entry.header_token):
            return Response(status_code=403)

        body = await _read_body(request, max_body_bytes)
        if body is None:
            return Response(status_code=413)

        if not entry.accepts_updates:
            # Бот или мастер отключён: 200, чтобы Telegram не повторял доставку. Апдейт до хендлеров не доходит.
            logger.info("Апдейт проигнорирован: бот %s неактивен (бот/мастер отключён)", entry.telegram_bot_id)
            if entry.bot is not None and entry.is_active and not entry.master_active:
                # Отключён мастер, а бот жив — можно коротко вежливо ответить. Если выключен сам бот (is_active = false),
                # Bot в реестре не создаётся: ответить нечем, апдейт отбрасывается молча.
                try:
                    await _politely_decline(entry, body, dp, session_factory)
                except Exception:
                    logger.exception("Не удалось ответить отключённому боту %s", entry.telegram_bot_id)
            return Response(status_code=200)

        try:
            update = Update.model_validate_json(body, context={"bot": entry.bot})
        except (ValidationError, ValueError):
            logger.warning("Апдейт бота %s не прошёл валидацию, пропущен", entry.telegram_bot_id)
            return Response(status_code=200)

        try:
            if admission is None:
                await dp.feed_update(entry.bot, update)
            else:
                async with admission.slot(entry.telegram_bot_id):
                    await dp.feed_update(entry.bot, update)
        except AdmissionTimeout:
            # Очередь этого бота (или общая) переполнена: не теряем апдейт, Telegram повторит доставку позже.
            logger.warning("Апдейт бота %s (master_id=%s) не получил места в очереди обработки: 503", entry.telegram_bot_id, entry.master_id)
            return Response(status_code=503, headers={"Retry-After": "5"})
        except Exception:
            logger.exception("Ошибка обработки апдейта бота %s (master_id=%s)", entry.telegram_bot_id, entry.master_id)
        return Response(status_code=200)

    if superadmin is not None:

        @app.post("/sa/{secret}")
        async def receive_superadmin(secret: str, request: Request) -> Response:
            # Как и для ботов мастеров: чужой секрет пути — 404 без тела, неверный заголовок — 403.
            if not _equal(secret, superadmin.path_secret or ""):
                return Response(status_code=404)
            if not _equal(request.headers.get(SECRET_HEADER, ""), superadmin.header_token or ""):
                return Response(status_code=403)
            body = await _read_body(request, max_body_bytes)
            if body is None:
                return Response(status_code=413)
            try:
                update = Update.model_validate_json(body, context={"bot": superadmin.bot})
            except (ValidationError, ValueError):
                logger.warning("Апдейт суперадмин-бота не прошёл валидацию, пропущен")
                return Response(status_code=200)
            try:
                await superadmin.dp.feed_update(superadmin.bot, update)
            except Exception:
                logger.exception("Ошибка обработки апдейта суперадмин-бота")
            return Response(status_code=200)

    return app
