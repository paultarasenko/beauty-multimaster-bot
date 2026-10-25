from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class MasterProfile(Base, TimestampMixin):
    """Визуальный профиль мастера: по одной строке на мастера (`master_id` уникален).

    Аккаунт мастера (владелец, активность) — в `masters`; здесь то, что видит клиент: имя, фото, адрес, приветствие.
    """

    __tablename__ = "master_profile"
    __table_args__ = (UniqueConstraint("master_id", name="uq_master_profile_master_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # None — имя удалено мастером
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    greeting_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # None — стандартное приветствие
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location_landmark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    map_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    contact_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    experience_years: Mapped[int | None] = mapped_column(nullable=True)
    specialization: Mapped[str | None] = mapped_column(String(500), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Minsk")