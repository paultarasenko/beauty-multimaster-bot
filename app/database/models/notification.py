import datetime as dt
import enum

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class NotificationType(str, enum.Enum):
    REMINDER_24H = "reminder_24h"
    REMINDER_2H_CLIENT = "reminder_2h_client"
    REMINDER_2H_MASTER = "reminder_2h_master"


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENDING = "sending"  # взято планировщиком в работу (этап 6): идёт отправка; зависшее возвращается в pending
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        # На одну запись — только одно напоминание данного типа. Защита от дублей
        # на уровне БД, а не "будем аккуратны в коде".
        UniqueConstraint("booking_id", "type", name="uq_notifications_booking_type"),
        # Уведомление принадлежит тому же мастеру, что и его запись (цель — uq_bookings_master_id_id).
        ForeignKeyConstraint(
            ["master_id", "booking_id"], ["bookings.master_id", "bookings.id"], name="fk_notifications_master_booking"
        ),
        # Выборка «созревших» уведомлений мастера планировщиком (этап 6) и поиск зависших в sending.
        Index("ix_notifications_master_due", "master_id", "scheduled_at", postgresql_where=text("status = 'pending'")),
        # Этап 12C: поиск зависших в sending идёт по мастеру, поэтому master_id — первый столбец (раньше был только claimed_at).
        Index("ix_notifications_master_sending", "master_id", "claimed_at", postgresql_where=text("status = 'sending'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False, default=NotificationType.REMINDER_24H.value)
    scheduled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=NotificationStatus.PENDING.value)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Когда планировщик взял уведомление в работу (status=sending); по нему возвращаем зависшие в pending.
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
