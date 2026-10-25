from sqlalchemy import CheckConstraint, ForeignKey, Integer, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class BreakPeriod(Base, TimestampMixin):
    """Перерыв внутри рабочего дня. Может быть несколько на один weekday."""

    __tablename__ = "breaks"
    __table_args__ = (
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_breaks_weekday_range"),
        CheckConstraint("start_time < end_time", name="ck_breaks_start_before_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[Time] = mapped_column(Time, nullable=False)
    end_time: Mapped[Time] = mapped_column(Time, nullable=False)
