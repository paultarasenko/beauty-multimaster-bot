from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin
from app.utils.master_settings import MAX_TEMPLATE_LEN


class MasterSettings(Base, TimestampMixin):
    """Индивидуальные настройки мастера (этап 8): по одной строке на мастера (`master_id` уникален).

    Значения по умолчанию равны поведению бота до этапа 8. Часовой пояс здесь НЕ хранится: он остаётся в
    `master_profile.timezone` (единственный источник правды). Приветствие клиенту тоже остаётся в
    `master_profile.greeting_text` (его редактирует кнопка «👋 Приветствие»). NULL в текстовых колонках —
    «текст по умолчанию из кода».
    """

    __tablename__ = "master_settings"
    __table_args__ = (
        UniqueConstraint("master_id", name="uq_master_settings_master_id"),
        CheckConstraint("slot_interval_minutes IN (15, 30, 60)", name="ck_master_settings_slot_interval"),
        CheckConstraint("max_active_bookings BETWEEN 1 AND 10", name="ck_master_settings_max_active_bookings"),
        CheckConstraint("cancel_streak_limit BETWEEN 2 AND 10", name="ck_master_settings_cancel_streak_limit"),
        CheckConstraint("char_length(currency) BETWEEN 1 AND 8", name="ck_master_settings_currency_len"),
        CheckConstraint(
            f"booking_confirmation_text IS NULL OR char_length(booking_confirmation_text) <= {MAX_TEMPLATE_LEN}",
            name="ck_master_settings_confirmation_len",
        ),
        CheckConstraint(
            f"cancellation_text IS NULL OR char_length(cancellation_text) <= {MAX_TEMPLATE_LEN}",
            name="ck_master_settings_cancellation_len",
        ),
        CheckConstraint(
            f"reminder_24h_text IS NULL OR char_length(reminder_24h_text) <= {MAX_TEMPLATE_LEN}",
            name="ck_master_settings_reminder_24h_len",
        ),
        CheckConstraint(
            f"reminder_2h_text IS NULL OR char_length(reminder_2h_text) <= {MAX_TEMPLATE_LEN}",
            name="ck_master_settings_reminder_2h_len",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False)

    slot_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")

    reminder_24h_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    reminder_2h_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    master_reminder_2h_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    max_active_bookings: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    cancel_streak_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")

    allow_reschedule: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    allow_reviews: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="BYN", server_default="BYN")

    booking_confirmation_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_24h_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_2h_text: Mapped[str | None] = mapped_column(Text, nullable=True)
