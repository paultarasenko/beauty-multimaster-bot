import datetime as dt

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Text, Time, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class MasterQuietMode(Base, TimestampMixin):
    """Тихий режим мастера: ежедневное окно, в течение которого бот не присылает мастеру уведомления.

    По одной строке на мастера. Время — настенные часы мастера (часовой пояс берётся из `master_profile.timezone`);
    окно может переходить через полночь (22:00–08:00). Отдельная таблица, а не колонки `master_settings`: тихий режим —
    личное состояние мастера, оно не входит в эталон, который «Обновить базу» копирует другим мастерам.
    """

    __tablename__ = "master_quiet_mode"
    __table_args__ = (
        UniqueConstraint("master_id", name="uq_master_quiet_mode_master_id"),
        CheckConstraint("start_time <> end_time", name="ck_master_quiet_mode_window"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False, default=dt.time(22, 0), server_default="22:00")
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False, default=dt.time(8, 0), server_default="08:00")


class MasterDeferredMessage(Base, TimestampMixin):
    """Сообщение мастеру, отложенное тихим режимом: уходит, когда тихий режим закончится (`created_at` — время события).

    `reply_markup` — inline-клавиатура сообщения в JSON (кнопки действуют и при доставке позже). `expires_at` — когда
    сообщение теряет смысл (напоминание «через 2 часа» после начала записи): просроченное не отправляется.
    """

    __tablename__ = "master_deferred_messages"
    __table_args__ = (Index("ix_master_deferred_messages_master_id_id", "master_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_markup: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
