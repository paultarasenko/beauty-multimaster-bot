"""Шифрование токенов ботов и вывод служебных секретов webhook.

Токен бота хранится в БД только зашифрованным (Fernet). Ключ — BOT_TOKEN_ENCRYPTION_KEY; сгенерировать:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Токен никогда не попадает в repr, исключения и логи этого модуля.
"""

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.config.settings import settings


class ConfigurationError(RuntimeError):
    """Неверная или отсутствующая настройка режима webhook (сообщение — для оператора, без секретов)."""


class TokenDecryptionError(RuntimeError):
    """Не удалось расшифровать токен (другой ключ или повреждённые данные). Текст токена в сообщении не раскрывается."""


KEY_HINT = (
    'Сгенерируйте ключ: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" '
    "и укажите его в BOT_TOKEN_ENCRYPTION_KEY."
)


class TokenCipher:
    def __init__(self, key: str):
        if not key or not key.strip():
            raise ConfigurationError(f"BOT_TOKEN_ENCRYPTION_KEY не задан. {KEY_HINT}")
        try:
            self._fernet = Fernet(key.strip().encode())
        except (ValueError, TypeError):
            raise ConfigurationError(f"BOT_TOKEN_ENCRYPTION_KEY некорректен (нужен ключ Fernet). {KEY_HINT}") from None
        # Ключ нужен ещё и для HMAC заголовка secret_token — отдельно от Fernet-объекта.
        self._hmac_key = key.strip().encode()

    @classmethod
    def from_settings(cls) -> "TokenCipher":
        return cls(settings.bot_token_encryption_key)

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, encrypted: str) -> str:
        try:
            return self._fernet.decrypt(encrypted.encode()).decode()
        except (InvalidToken, ValueError):
            raise TokenDecryptionError("не удалось расшифровать токен бота (проверьте BOT_TOKEN_ENCRYPTION_KEY)") from None

    def header_token(self, webhook_secret: str) -> str:
        """Значение X-Telegram-Bot-Api-Secret-Token для бота с данным webhook_secret.

        Выводится из секрета пути через HMAC с ключом шифрования, поэтому знание одного только пути (он остаётся в логах
        обратного прокси) не позволяет подделать заголовок. Hex-строка допустима для Telegram (A-Z, a-z, 0-9, _ и -).
        """
        return hmac.new(self._hmac_key, b"tg-secret-token:" + webhook_secret.encode(), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "TokenCipher(<key hidden>)"


def generate_webhook_secret() -> str:
    """Случайный секрет для пути webhook: 43 символа [A-Za-z0-9_-], безопасен для URL."""
    return secrets.token_urlsafe(32)
