import datetime as dt

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class BlockedPeriod(Base, TimestampMixin):
    __tablename__ = "blocked_periods"
    __table_args__ = (
        CheckConstraint("start_datetime < end_datetime", name="ck_blocked_periods_start_before_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    start_datetime: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_datetime: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
