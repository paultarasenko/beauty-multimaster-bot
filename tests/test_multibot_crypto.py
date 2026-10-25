"""Шифрование токенов ботов и маскирование секретов."""

import logging

import pytest
from cryptography.fernet import Fernet

from app.database.models import MasterBot
from app.multibot.crypto import ConfigurationError, TokenCipher, TokenDecryptionError, generate_webhook_secret
from app.multibot.redact import SecretRedactingFilter, redact
from tests.webhook_utils import token_for

TOKEN = token_for(424242)


def _cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key().decode())


def test_round_trip_and_ciphertext_hides_token():
    cipher = _cipher()
    encrypted = cipher.encrypt(TOKEN)
    assert TOKEN not in encrypted and "424242" not in encrypted
    assert cipher.decrypt(encrypted) == TOKEN


def test_wrong_key_raises_without_leaking_token():
    encrypted = _cipher().encrypt(TOKEN)
    with pytest.raises(TokenDecryptionError) as exc:
        _cipher().decrypt(encrypted)
    assert TOKEN not in str(exc.value) and encrypted not in str(exc.value)


@pytest.mark.parametrize("key", ["", "   ", "not-a-fernet-key"])
def test_missing_or_invalid_key_gives_clear_error(key):
    with pytest.raises(ConfigurationError, match="BOT_TOKEN_ENCRYPTION_KEY"):
        TokenCipher(key)


def test_repr_hides_key_and_model_repr_hides_token_and_secret():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    assert key not in repr(cipher)
    row = MasterBot(
        id=1, master_id=1, telegram_bot_id=424242, bot_username="x_bot",
        bot_token_encrypted=cipher.encrypt(TOKEN), webhook_secret="SECRETVALUE", is_active=True,
    )
    text = repr(row)
    assert TOKEN not in text and row.bot_token_encrypted not in text and "SECRETVALUE" not in text


def test_header_token_is_derived_and_differs_from_path_secret():
    cipher = _cipher()
    secret = generate_webhook_secret()
    header = cipher.header_token(secret)
    assert header != secret and header == cipher.header_token(secret)
    assert header != _cipher().header_token(secret)
    assert len(header) <= 256 and all(c in "0123456789abcdef" for c in header)


def test_generated_secrets_are_unique_and_url_safe():
    secrets_ = {generate_webhook_secret() for _ in range(50)}
    assert len(secrets_) == 50
    assert all(all(c.isalnum() or c in "-_" for c in s) for s in secrets_)


def test_redact_masks_token_and_webhook_path():
    assert TOKEN not in redact(f"error calling https://api.telegram.org/bot{TOKEN}/getMe")
    assert "SECRET123" not in redact("POST /tg/424242/SECRET123 HTTP/1.1")
    assert "topsecret" not in redact("value topsecret here", "topsecret")


def test_logging_filter_masks_token_in_message_and_traceback(caplog):
    logger = logging.getLogger("tests.redaction")
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactingFilter())
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(Capture.formatter.format(record))

    Capture.formatter = logging.Formatter("%(message)s")
    capture = Capture()
    capture.addFilter(SecretRedactingFilter())
    logger.addHandler(capture)
    try:
        try:
            raise RuntimeError(f"boom {TOKEN}")
        except RuntimeError:
            logger.error("failed with %s", TOKEN, exc_info=True)
    finally:
        logger.removeHandler(capture)
    assert records and TOKEN not in records[0]
