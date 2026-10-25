from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class WorkingHours(Base, TimestampMixin):
    """Расписание по дням недели. weekday: 0=Monday ... 6=Sunday (ISO, как в Python date.weekday())."""

    __tablename__ = "working_hours"
    __table_args__ = (
        UniqueConstraint("master_id", "weekday", name="uq_working_hours_master_weekday"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_working_hours_weekday_range"),
        CheckConstraint(
            "is_working = false OR start_time < end_time", name="ck_working_hours_start_before_end"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    is_working: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    start_time: Mapped[Time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[Time | None] = mapped_column(Time, nullable=True)
