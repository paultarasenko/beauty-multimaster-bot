import datetime as dt

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class WorkingDayOverride(Base, TimestampMixin):
    """Исключение из недельного расписания на одну дату: открыть обычно выходной день или закрыть рабочий.

    Главнее WorkingHours: если на дату есть строка, рабочие часы дня берутся из неё, иначе — из расписания по дням недели.
    is_working=False — день закрыт целиком (start_time/end_time пусты).
    """

    __tablename__ = "working_day_overrides"
    __table_args__ = (
        UniqueConstraint("master_id", "day", name="uq_working_day_overrides_master_day"),
        CheckConstraint(
            "is_working = false OR (start_time IS NOT NULL AND end_time IS NOT NULL AND start_time < end_time)",
            name="ck_working_day_overrides_hours",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    is_working: Mapped[bool] = mapped_column(Boolean, nullable=False)
    start_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
