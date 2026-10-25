import datetime as dt
import enum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class AccessRequestStatus(str, enum.Enum):
    PENDING = "pending"  # ждёт решения мастера
    OPENED = "opened"  # мастер открыл доступ
    BLOCKED = "blocked"  # мастер ограничил доступ навсегда


class ClientAccessRequest(Base, TimestampMixin):
    """Обращение ограниченного клиента к мастеру («📞 Связаться со мной»). Одно сообщение — одно обращение.

    Хранится в БД, а не в памяти процесса: кнопки мастера работают и после рестарта бота.
    """

    __tablename__ = "client_access_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'opened', 'blocked')",
            name="ck_client_access_requests_status",
        ),
        # У клиента к одному мастеру одновременно не больше одного ожидающего обращения (защита и от гонки двойной отправки).
        Index(
            "uq_client_access_requests_one_pending",
            "master_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AccessRequestStatus.PENDING.value, server_default="pending"
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
