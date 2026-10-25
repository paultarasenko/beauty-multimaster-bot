"""Периодическая проверка ботов мастеров (RUN_MODE=webhook): токен (getMe) и webhook (getWebhookInfo).

Раз в SUPERADMIN_HEALTH_INTERVAL_SECONDS обходит активные боты активных мастеров и сообщает суперадмину:
- токен отвергнут Telegram (Unauthorized);
- webhook не установлен или указывает не на наш адрес;
- Telegram сообщает свежую ошибку доставки апдейтов (наш сервер не отвечает/отвечает ошибкой).
Одиночный сбой связи с Telegram — не проблема бота: пишется в лог, суперадмину не отправляется.
Последнее известное состояние каждого бота доступно суперадмин-боту (`state_of`) для списка мастеров.
"""

import asyncio
import datetime as dt
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.multibot import management
from app.multibot.crypto import ConfigurationError, TokenCipher, TokenDecryptionError
from app.superadmin import alerts as alert_texts
from app.superadmin.alerts import AlertNotifier
from app.superadmin.service import list_active_bot_targets

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_PROBLEM = "problem"
STATE_UNKNOWN = "unknown"
CHECK_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENT_CHECKS = 5
# Свежей считается ошибка доставки, случившаяся не раньше, чем за столько интервалов проверки.
FRESH_ERROR_INTERVALS = 2


class WebhookHealthMonitor:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        cipher: TokenCipher,
        alerts: AlertNotifier,
        *,
        public_base_url: str,
        interval_seconds: float,
        bot_factory=management.default_bot_factory,
        auto_refresh: bool = False,
    ):
        self._session_factory = session_factory
        self._cipher = cipher
        self._alerts = alerts
        self._public_base_url = public_base_url
        self._interval = interval_seconds
        self._bot_factory = bot_factory
        self._auto_refresh = auto_refresh
        self._states: dict[int, str] = {}  # telegram_bot_id -> STATE_*
        self._task: asyncio.Task | None = None

    # ---- состояние для интерфейса ----

    def state_of(self, telegram_bot_id: int | None) -> str | None:
        """Последнее известное состояние бота или None, если проверки ещё не было."""
        return None if telegram_bot_id is None else self._states.get(telegram_bot_id)

    # ---- жизненный цикл ----

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="superadmin-health")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Проверка ботов мастеров: непредвиденный сбой, повтор через интервал")
            await asyncio.sleep(self._interval)

    # ---- одна проверка ----

    async def run_once(self) -> None:
        targets = await list_active_bot_targets(self._session_factory)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

        async def guarded(master_id: int, bot_id: int) -> None:
            async with semaphore:
                await self._check_one(master_id, bot_id)

        await asyncio.gather(*(guarded(m, b) for m, b in targets))
        known = {bot_id for _, bot_id in targets}
        for bot_id in set(self._states) - known:  # бот удалён/отключён — состояние больше не показываем
            self._states.pop(bot_id, None)

    async def _check_one(self, master_id: int, bot_id: int) -> None:
        try:
            check = await asyncio.wait_for(
                management.check_bot(
                    self._session_factory,
                    self._cipher,
                    master_id=master_id,
                    public_base_url=self._public_base_url,
                    bot_factory=self._bot_factory,
                ),
                CHECK_TIMEOUT_SECONDS,
            )
        except (management.ManagementError, TokenDecryptionError, ConfigurationError, asyncio.TimeoutError) as exc:
            logger.warning("Проверка бота мастера %s (bot_id=%s) не выполнена: %s", master_id, bot_id, type(exc).__name__)
            self._states[bot_id] = STATE_UNKNOWN
            return
        await self.apply(check)

    async def apply(self, check: management.BotCheck) -> None:
        """Сопоставляет результат проверки с состоянием и при необходимости отправляет сигнал (с защитой от повторов)."""
        bot_id, master_id, name = check.telegram_bot_id, check.master_id, check.bot_username
        if check.token_valid is False:
            self._states[bot_id] = STATE_PROBLEM
            await self._alerts.notify(
                alert_texts.KIND_INVALID_TOKEN, bot_id, alert_texts.invalid_token_text(master_id, bot_id, name)
            )
            return
        self._alerts.clear(alert_texts.KIND_INVALID_TOKEN, bot_id)
        if check.error is not None:
            # Нет связи с Telegram или getWebhookInfo не ответил: состояние неизвестно, не тревожим суперадмина.
            logger.warning("Проверка бота мастера %s (bot_id=%s) не состоялась: %s", master_id, bot_id, check.error)
            self._states[bot_id] = STATE_UNKNOWN
            return

        problem = self._webhook_problem(check)
        if problem is None:
            self._states[bot_id] = STATE_OK
            self._alerts.clear(alert_texts.KIND_WEBHOOK_DOWN, bot_id)
            return
        if self._auto_refresh and (check.webhook_registered is False or check.webhook_matches is False):
            # Адрес изменился (например, перезапуск туннеля) или webhook пропал: ставим заново сами, без кнопки в карточке мастера.
            if await self._refresh_webhook(master_id, bot_id):
                self._states[bot_id] = STATE_OK
                self._alerts.clear(alert_texts.KIND_WEBHOOK_DOWN, bot_id)
                return
        self._states[bot_id] = STATE_PROBLEM
        await self._alerts.notify(
            alert_texts.KIND_WEBHOOK_DOWN, bot_id, alert_texts.webhook_down_text(master_id, bot_id, name, problem)
        )

    async def _refresh_webhook(self, master_id: int, bot_id: int) -> bool:
        """Переустанавливает webhook бота мастера на текущий PUBLIC_BASE_URL; False — не удалось (тогда уйдёт обычный сигнал)."""
        try:
            await asyncio.wait_for(
                management.refresh_webhook(
                    self._session_factory,
                    self._cipher,
                    master_id=master_id,
                    public_base_url=self._public_base_url,
                    bot_factory=self._bot_factory,
                ),
                CHECK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "Автоматическая переустановка webhook мастера %s (bot_id=%s) не удалась: %s", master_id, bot_id, type(exc).__name__
            )
            return False
        logger.info("Webhook бота мастера %s (bot_id=%s) переустановлен автоматически", master_id, bot_id)
        return True

    def _webhook_problem(self, check: management.BotCheck) -> str | None:
        if check.webhook_registered is False:
            return "У Telegram не установлен webhook: апдейты бота не доставляются."
        if check.webhook_matches is False:
            return "Webhook установлен на другой адрес (смена PUBLIC_BASE_URL или секрета)."
        if check.last_error_message and check.last_error_at is not None:
            age = (dt.datetime.now(dt.timezone.utc) - check.last_error_at).total_seconds()
            if age <= self._interval * FRESH_ERROR_INTERVALS:
                return f"Telegram не может доставить апдейты: {check.last_error_message}"
        return None
