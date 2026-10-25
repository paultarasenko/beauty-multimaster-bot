from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class MasterBot(Base, TimestampMixin):
    """Telegram-бот мастера (мульти-бот, этап 4): один бот на мастера.

    Токен хранится ТОЛЬКО в зашифрованном виде (Fernet, ключ BOT_TOKEN_ENCRYPTION_KEY) и не попадает ни в repr,
    ни в логи. `webhook_secret` — часть пути webhook (/tg/{telegram_bot_id}/{secret}); значение заголовка
    X-Telegram-Bot-Api-Secret-Token выводится из него отдельно (см. app/multibot/crypto.py).
    """

    __tablename__ = "master_bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), unique=True, nullable=False)
    # id бота в Telegram (число до двоеточия в токене): по нему Telegram-апдейт находит бота и мастера.
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    bot_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    def __repr__(self) -> str:
        # Намеренно без токена и секрета.
        return (
            f"MasterBot(id={self.id!r}, master_id={self.master_id!r}, telegram_bot_id={self.telegram_bot_id!r}, "
            f"bot_username={self.bot_username!r}, is_active={self.is_active!r})"
        )
