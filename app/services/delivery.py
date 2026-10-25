"""Классификация ошибок отправки в Telegram для планировщика (этап 6).

Что делать с ошибкой, решает не тот, кто отправляет, а единая таблица здесь:

- `PERMANENT` — сообщение не доставить никогда (клиент заблокировал бота — `TelegramForbiddenError`, чат не найден и т.п.):
  уведомление становится FAILED, повторов нет;
- `UNKNOWN` — любая другая ошибка (баг в тексте, неожиданное исключение): как и раньше, FAILED без повторов — лучше потерять
  одно напоминание, чем слать его бесконечно;
- `STOP_UNAUTHORIZED` — токен бота недействителен: уведомление остаётся в очереди, обработка мастера на этом тике
  прекращается, бот помечается неактивным (см. `SchedulerService`);
- `STOP_RETRY_AFTER` — Telegram просит подождать (`TelegramRetryAfter`): уведомление остаётся в очереди, бот на это время
  «в паузе», остальные боты не затронуты;
- `STOP_TRANSIENT` — сеть/таймаут/5xx: уведомление остаётся в очереди и уйдёт на одном из следующих тиков.

`STOP_*` означают «у этого бота сейчас не получится ничего»: продолжать перебор его уведомлений бессмысленно (и вредно — это
и есть шторм запросов), поэтому сервисы поднимают `DeliveryStop` после того, как вернули текущую запись в очередь.
"""

import asyncio
import enum

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

DEFAULT_RETRY_AFTER_SECONDS = 30.0  # если Telegram не сообщил срок / при сетевой ошибке: пауза бота перед следующей попыткой


class Outcome(str, enum.Enum):
    PERMANENT = "permanent"
    UNKNOWN = "unknown"
    STOP_UNAUTHORIZED = "stop_unauthorized"
    STOP_RETRY_AFTER = "stop_retry_after"
    STOP_TRANSIENT = "stop_transient"

    @property
    def stops_bot(self) -> bool:
        return self in (Outcome.STOP_UNAUTHORIZED, Outcome.STOP_RETRY_AFTER, Outcome.STOP_TRANSIENT)


class DeliveryStop(Exception):
    """Отправка этим ботом сейчас невозможна: остальные уведомления мастера на этом тике не трогаем.

    Поднимается сервисами ПОСЛЕ того, как текущая запись возвращена в очередь (ничего не потеряно).
    """

    def __init__(self, outcome: Outcome, retry_after: float | None = None, cause: BaseException | None = None):
        super().__init__(outcome.value)
        self.outcome = outcome
        self.retry_after = retry_after
        self.cause = cause


def classify_send_error(exc: BaseException) -> Outcome:
    if isinstance(exc, TelegramUnauthorizedError):
        return Outcome.STOP_UNAUTHORIZED
    if isinstance(exc, TelegramRetryAfter):
        return Outcome.STOP_RETRY_AFTER
    if isinstance(exc, (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError, ConnectionError)):
        return Outcome.STOP_TRANSIENT
    if isinstance(exc, (TelegramForbiddenError, TelegramBadRequest, TelegramNotFound, TelegramMigrateToChat)):
        return Outcome.PERMANENT
    return Outcome.UNKNOWN


def stop_from(exc: BaseException, outcome: Outcome) -> DeliveryStop:
    retry_after = None
    if isinstance(exc, TelegramRetryAfter):
        retry_after = float(exc.retry_after)
    elif outcome is Outcome.STOP_TRANSIENT:
        retry_after = DEFAULT_RETRY_AFTER_SECONDS
    return DeliveryStop(outcome, retry_after, exc)
