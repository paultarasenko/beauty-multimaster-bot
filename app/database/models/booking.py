import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class BookingStatus(str, enum.Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class Booking(Base, TimestampMixin):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("end_datetime > start_datetime", name="ck_bookings_end_after_start"),
        # Главная защита от double booking — на уровне PostgreSQL.
        # time_range — сгенерированная БД колонка tstzrange(start, end, '[)').
        # GiST exclusion constraint не даёт вставить/обновить строку, чей time_range
        # пересекается с time_range другой строки того же мастера со status='confirmed'
        # (у разных мастеров одно и то же время — не конфликт).
        # Работает даже при гонке параллельных транзакций — в отличие от проверки
        # "SELECT свободно ли -> INSERT" в коде приложения.
        ExcludeConstraint(
            ("master_id", "="),
            ("time_range", "&&"),
            name="ex_bookings_no_overlap_confirmed",
            postgresql_using="gist",
            postgresql_where=text("status = 'confirmed'"),
        ),
        # Запись и её услуга принадлежат одному мастеру (цель — uq_services_master_id_id).
        ForeignKeyConstraint(
            ["master_id", "service_id"], ["services.master_id", "services.id"], name="fk_bookings_master_service"
        ),
        # Цель составного FK из notifications (master_id, booking_id).
        UniqueConstraint("master_id", "id", name="uq_bookings_master_id_id"),
        # Этап 12C. Записи клиента у мастера (счётчики активных/выполненных, «Мои записи», история).
        Index("ix_bookings_master_user_start", "master_id", "user_id", "start_datetime"),
        # Этап 12C. Проверка занятости (свободные слоты) и завершение прошедших записей: только подтверждённые, по концу записи.
        Index("ix_bookings_master_end_confirmed", "master_id", "end_datetime", postgresql_where=text("status = 'confirmed'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"), nullable=False)

    start_datetime: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_datetime: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    time_range = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(start_datetime, end_datetime, '[)')", persisted=True),
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=BookingStatus.CONFIRMED.value)

    # Snapshot данных услуги на момент записи — цена/длительность/название могли
    # измениться в services позже, но историческая запись должна остаться такой,
    # какой она была при бронировании.
    service_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    service_price_snapshot: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    service_duration_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    # Свободное время после услуги на момент записи: сколько минут после end_datetime слот занят для других.
    service_buffer_snapshot: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # Запись отменил сам клиент (а не мастер и не перенос) — только такие отмены идут в счёт «серии отмен».
    cancelled_by_client: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Ссылка на запись, которую эта заменила при переносе (self-reference).
    previous_booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )

    # Данные клиента, если запись создана мастером вручную (клиент без Telegram-записи).
    manual_client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manual_client_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Одноразовый токен ссылки-приглашения в бота (t.me/<бот>?start=inv_<токен>). Есть только у записи,
    # которую мастер создал для человека, ещё не писавшего боту: когда тот откроет ссылку, запись
    # привяжется к нему (user_id) и токен обнулится.
    invite_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)

    @property
    def occupied_minutes(self) -> int:
        """Длительность записи вместе со свободным временем после неё (для переноса записи)."""
        return self.service_duration_snapshot + (self.service_buffer_snapshot or 0)

    @property
    def busy_end_datetime(self) -> dt.datetime:
        """Момент, до которого время занято для других клиентов: конец услуги + свободное время."""
        return self.end_datetime + dt.timedelta(minutes=self.service_buffer_snapshot or 0)
