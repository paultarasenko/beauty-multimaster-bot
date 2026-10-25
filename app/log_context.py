"""Контекст обработки апдейта для логов: чей это мастер и какой бот принял апдейт (этап 5).

`ErrorHandlingMiddleware` (самый внешний на dp.update) открывает контекст и кладёт id бота, `TenantMiddleware` дописывает
`master_id`, когда мастер определён. Контекст — изменяемый объект в contextvar, поэтому строка лога, написанная
внешним middleware ПОСЛЕ того, как внутренний уже отработал (например, `logger.exception` при падении хендлера), всё
равно знает мастера. Каждая asyncio-задача (апдейт) видит свой контекст — в параллельных апдейтах он не смешивается.

`LogContextFilter` дописывает к каждой записи поля `master_id` и `bot_id` (`-`, если записи не относятся к апдейту:
старт приложения, планировщик, CLI). Секретов в контексте нет: только числовые идентификаторы.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

LOG_FORMAT = "%(asctime)s | %(levelname)s | m=%(master_id)s b=%(bot_id)s | %(name)s | %(message)s"


@dataclass
class UpdateLogContext:
    master_id: int | None = None
    bot_id: int | None = None


_current: ContextVar[UpdateLogContext | None] = ContextVar("update_log_context", default=None)


def current_context() -> UpdateLogContext | None:
    return _current.get()


@contextmanager
def update_log_context(bot_id: int | None = None) -> Iterator[UpdateLogContext]:
    """Открывает контекст апдейта; по выходу возвращает прежний (вложенные вызовы не затирают друг друга)."""
    context = UpdateLogContext(bot_id=bot_id)
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


@contextmanager
def ensure_log_context(bot_id: int | None = None) -> Iterator[UpdateLogContext]:
    """Контекст апдейта: уже открытый внешним middleware (его и возвращает) или, если внешнего нет, собственный
    на время работы блока — контекст не «протекает» в следующий апдейт той же задачи."""
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    with update_log_context(bot_id=bot_id) as context:
        yield context


class LogContextFilter(logging.Filter):
    """Добавляет к записи `master_id` и `bot_id` из контекста текущего апдейта (или «-»)."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _current.get()
        master_id = context.master_id if context is not None else None
        bot_id = context.bot_id if context is not None else None
        record.master_id = "-" if master_id is None else master_id
        record.bot_id = "-" if bot_id is None else bot_id
        return True


def install_log_context(root: logging.Logger | None = None) -> None:
    """Вешает фильтр на все обработчики корневого логгера (поля нужны в формате `LOG_FORMAT`). Повторный вызов безопасен."""
    for handler in (root or logging.getLogger()).handlers:
        if not any(isinstance(f, LogContextFilter) for f in handler.filters):
            handler.addFilter(LogContextFilter())


def configure_logging(level: int = logging.INFO) -> None:
    """Единая настройка логов приложения: формат с master_id/bot_id и фильтр контекста."""
    logging.basicConfig(level=level, format=LOG_FORMAT)
    install_log_context()
