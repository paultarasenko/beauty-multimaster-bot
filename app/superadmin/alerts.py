"""Уведомления суперадмину о проблемах ботов мастеров (этап 9). Без спама.

Событие определяется парой (вид, ключ): например («invalid_token», id бота). Об одном и том же событии уведомление уходит
не чаще, чем раз в `interval_seconds`; когда проблема исчезла (`clear`), следующее появление снова сообщается сразу.
Состояние хранится в объекте (в памяти процесса): после перезапуска сервиса о ещё не устранённой проблеме
напомнят один раз заново — это осознанное упрощение. В тексте только числовые id и @username, без токенов и секретов.
"""

import logging
import time
from collections.abc import Callable, Iterable

from aiogram import Bot

from app.multibot.redact import redact

logger = logging.getLogger(__name__)

KIND_INVALID_TOKEN = "invalid_token"
KIND_WEBHOOK_DOWN = "webhook_down"
KIND_CONNECTION_ERROR = "connection_error"

MAX_TRACKED_EVENTS = 2048
SEND_TIMEOUT_SECONDS = 10.0


def describe_bot(master_id: int | None, bot_id: int | None, username: str | None = None) -> str:
    parts = []
    if master_id is not None:
        parts.append(f"мастер №{master_id}")
    if username:
        parts.append(f"@{username}")
    if bot_id is not None:
        parts.append(f"бот id {bot_id}")
    return ", ".join(parts) or "бот не определён"


def invalid_token_text(master_id: int | None, bot_id: int | None, username: str | None = None) -> str:
    return (
        f"🔴 Недействительный токен бота ({describe_bot(master_id, bot_id, username)}).\n"
        "Telegram отвергает токен: бот не может принимать и отправлять сообщения (планировщик помечает такой бот неактивным). "
        "Откройте карточку мастера → «Удалить бота», затем подключите бота с новым токеном."
    )


def webhook_down_text(master_id: int | None, bot_id: int | None, username: str | None, reason: str) -> str:
    return f"🟠 Webhook бота не работает ({describe_bot(master_id, bot_id, username)}).\n{reason}\nКарточка мастера → «Перерегистрировать webhook»."


def connection_error_text(master_id: int | None, bot_id: int | None, username: str | None, reason: str) -> str:
    return f"🟠 Ошибка подключения бота ({describe_bot(master_id, bot_id, username)}).\n{reason}"


class AlertNotifier:
    """Отправляет сигналы всем суперадминистраторам через суперадмин-бота с защитой от повторов."""

    def __init__(
        self,
        bot: Bot,
        recipients: Iterable[int],
        interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._bot = bot
        self._recipients = tuple(recipients)
        self._interval = interval_seconds
        self._clock = clock
        self._last_sent: dict[tuple[str, int], float] = {}

    def _due(self, event: tuple[str, int]) -> bool:
        last = self._last_sent.get(event)
        return last is None or self._clock() - last >= self._interval

    def clear(self, kind: str, key: int) -> None:
        """Проблема устранена: следующее появление этого события будет сообщено сразу."""
        self._last_sent.pop((kind, key), None)

    async def notify(self, kind: str, key: int, text: str) -> bool:
        """Отправляет сигнал, если об этом событии ещё не сообщали в течение интервала. True — хотя бы одному адресату доставлено.

        Никогда не выбрасывает исключений: сбой уведомления не должен мешать работе планировщика и проверок."""
        event = (kind, key)
        if not self._due(event):
            return False
        if len(self._last_sent) >= MAX_TRACKED_EVENTS:  # защита от разрастания: забываем устаревшие записи
            now = self._clock()
            self._last_sent = {e: t for e, t in self._last_sent.items() if now - t < self._interval}
        previous = self._last_sent.get(event)
        self._last_sent[event] = self._clock()  # помечаем ДО отправки: параллельный вызов с тем же событием не продублирует
        delivered = False
        for chat_id in self._recipients:
            try:
                await self._bot.send_message(chat_id, redact(text), request_timeout=SEND_TIMEOUT_SECONDS)
                delivered = True
            except Exception as exc:
                # Например, суперадмин ещё не нажимал /start у служебного бота (Forbidden) или нет связи с Telegram.
                logger.warning("Не удалось отправить уведомление суперадмину %s: %s", chat_id, type(exc).__name__)
        if not delivered:
            # Никому не доставлено — не считаем событие отправленным, повторим при следующей проверке.
            if previous is None:
                self._last_sent.pop(event, None)
            else:
                self._last_sent[event] = previous
        return delivered
