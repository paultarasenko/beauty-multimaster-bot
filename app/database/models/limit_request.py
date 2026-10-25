import datetime as dt
import enum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class LimitRequestStatus(str, enum.Enum):
    PENDING = "pending"  # ждёт решения мастера
    APPROVED = "approved"  # мастер разрешил одну запись сверх лимита
    REJECTED = "rejected"  # мастер отказал


class BookingLimitRequest(Base, TimestampMixin):
    """Обращение клиента к мастеру, когда у него уже максимум активных записей.

    Хранится в БД, а не в памяти процесса: кнопки мастера работают и после рестарта бота.
    Одобренный запрос — разовое разрешение на одну запись сверх лимита: override_used_at
    заполняется, когда клиент им воспользовался.
    """

    __tablename__ = "booking_limit_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_booking_limit_requests_status",
        ),
        # У клиента к одному мастеру одновременно не больше одного ожидающего запроса (защита и от гонки двойного нажатия).
        Index(
            "uq_booking_limit_requests_one_pending",
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
        String(20), nullable=False, default=LimitRequestStatus.PENDING.value, server_default="pending"
    )
    master_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    override_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
