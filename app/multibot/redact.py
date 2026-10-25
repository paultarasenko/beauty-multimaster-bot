"""Маскирование токенов ботов и секретов webhook в тексте (логи, сообщения об ошибках, вывод CLI)."""

import logging
import re

# Формат токена Telegram: <id бота>:<35 символов [A-Za-z0-9_-]>.
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
# Путь webhook: /tg/<id>/<secret> — секрет в логах не нужен.
_WEBHOOK_PATH_RE = re.compile(r"(/tg/\d+/)[A-Za-z0-9_-]+")


def redact(text: str, *extra_secrets: str) -> str:
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "***")
    text = _WEBHOOK_PATH_RE.sub(r"\1***", text)
    return _TOKEN_RE.sub("<bot-token>", text)


class SecretRedactingFilter(logging.Filter):
    """Страховка: даже если токен или путь webhook случайно попадут в сообщение лога (например, в тексте сетевой ошибки
    с URL запроса), в вывод они не уйдут."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info:
            # Текст исключения форматируется позже — приводим его к тексту сейчас, уже замаскированному.
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted)
        return True


def install_log_redaction() -> None:
    """Вешает фильтр на все обработчики корневого логгера (вызывается при старте webhook-режима и CLI)."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
