import datetime as dt

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base

RESULT_OK = "ok"
RESULT_ERROR = "error"


class SuperadminAction(Base):
    """Журнал действий суперадмин-бота (этап 9): кто, что, над каким мастером/ботом, когда и с каким результатом.

    Это служебная таблица владельца сервиса, а не данные мастера: колонка называется `target_master_id` (не `master_id`)
    и намеренно БЕЗ внешнего ключа — запись журнала должна пережить мастера (например, после ручной чистки БД).
    Токенов, секретов и персональных данных клиентов здесь нет: `detail` — короткий замаскированный текст.
    """

    __tablename__ = "superadmin_actions"
    __table_args__ = (
        CheckConstraint("result IN ('ok', 'error')", name="ck_superadmin_actions_result"),
        Index("ix_superadmin_actions_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_master_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # id бота в Telegram
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
