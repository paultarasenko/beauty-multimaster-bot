"""Контекст логов: master_id и bot_id в записях обработки апдейта (этап 5)."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.types import Message

from app.bot.middlewares.error_handling import GENERIC_ERROR_TEXT
from app.log_context import (
    LOG_FORMAT,
    LogContextFilter,
    current_context,
    ensure_log_context,
    install_log_context,
    update_log_context,
)
from app.multibot.redact import SecretRedactingFilter
from tests.tg import BOT_ID, tg_harness


def _record(msg: str = "m") -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)


def test_filter_without_context_marks_dashes():
    record = _record()
    LogContextFilter().filter(record)
    assert (record.master_id, record.bot_id) == ("-", "-")
    assert LOG_FORMAT % {**record.__dict__, "asctime": "t", "levelname": "INFO", "message": "m"}  # формат собирается


def test_context_sets_ids_and_is_restored():
    assert current_context() is None
    with update_log_context(bot_id=11) as outer:
        outer.master_id = 5
        record = _record()
        LogContextFilter().filter(record)
        assert (record.master_id, record.bot_id) == (5, 11)
        with ensure_log_context(bot_id=99) as inner:  # внешний контекст есть — используется он, bot_id не перетирается
            assert inner is outer
    assert current_context() is None


async def test_parallel_updates_do_not_mix_contexts():
    seen = {}

    async def update(master_id: int, bot_id: int):
        with update_log_context(bot_id=bot_id) as ctx:
            ctx.master_id = master_id
            await asyncio.sleep(0.01)  # другие апдейты успевают выставить свой контекст
            record = _record()
            LogContextFilter().filter(record)
            seen[master_id] = (record.master_id, record.bot_id)

    await asyncio.gather(*(update(m, 1000 + m) for m in range(1, 6)))
    assert seen == {m: (m, 1000 + m) for m in range(1, 6)}


def test_install_is_idempotent_and_coexists_with_redaction():
    logger = logging.getLogger("stage5-test-root")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        handler.addFilter(SecretRedactingFilter())
        install_log_context(logger)
        install_log_context(logger)
        assert sum(isinstance(f, LogContextFilter) for f in handler.filters) == 1
    finally:
        logger.removeHandler(handler)


def _crashing_router() -> Router:
    router = Router(name="crash5")

    @router.message(F.text == "boom")
    async def boom(message: Message):
        raise RuntimeError("секретные детали 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi чужие данные")

    return router


async def test_error_record_carries_master_and_bot_and_user_sees_only_generic_text(session, caplog):
    caplog.handler.addFilter(LogContextFilter())
    async with tg_harness(_crashing_router(), error_middleware=True, master_id=1) as h:
        await h.send_text(555, "boom")
        shown = h.tg.everything_shown()
    record = next(r for r in caplog.records if "Unhandled error" in r.getMessage())
    assert (record.master_id, record.bot_id) == (1, BOT_ID)
    assert shown == [GENERIC_ERROR_TEXT]  # без деталей исключения и чужих данных
    assert current_context() is None  # контекст закрыт вместе с апдейтом
