import datetime as dt
import enum

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class Recurrence(str, enum.Enum):
    ONCE = "once"  # «Один раз»
    DAILY = "daily"  # «Каждый день»
    WEEKLY = "weekly"  # «Раз в неделю» — в тот же день недели, что и anchor_date
    MONTHLY = "monthly"  # «Раз в месяц» — в то же число, что и anchor_date
    YEARLY = "yearly"  # «Каждый год» — каждый год в тот же день и месяц, что и anchor_date


class AdminReminder(Base, TimestampMixin):
    """Личное напоминание мастера самому себе (не связано с записями клиентов — для этого есть notifications).

    Источник истины — эта таблица, а не память процесса: напоминания переживают рестарт бота.
    Одноразовое напоминание удаляется сразу после отправки, повторяющееся — переносится на
    следующее срабатывание (next_fire_at).
    """

    __tablename__ = "admin_reminders"
    __table_args__ = (
        CheckConstraint(
            "recurrence IN ('once', 'daily', 'weekly', 'monthly', 'yearly')",
            name="ck_admin_reminders_recurrence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    recurrence: Mapped[str] = mapped_column(String(20), nullable=False)
    # Первая дата и время по настенным часам мастера. Из них считаются день недели / число / месяц повтора.
    anchor_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    remind_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    # Ближайшее срабатывание в UTC. По нему воркер находит due-напоминания.
    next_fire_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
