import datetime as dt

from sqlalchemy import BigInteger, Boolean, Date
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class Master(Base, TimestampMixin):
    """Аккаунт мастера — арендатор (tenant) системы.

    Все данные мастера (услуги, записи, расписание, отзывы, портфолио, уведомления и т.д.) принадлежат
    ему через `master_id`. Визуальный профиль (имя, фото, адрес…) лежит отдельно — в `master_profile`
    (по одной строке на мастера), настройки бота и поведения появятся на следующих этапах.
    """

    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Telegram-аккаунт владельца: по нему определяются права администратора и получатель уведомлений мастера.
    # Первоначально заполняется из ADMIN_TELEGRAM_ID (миграция мастера №1 или bootstrap при старте), дальше — источник истины.
    owner_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Пробный период (этап 10): последний день срока ВКЛЮЧИТЕЛЬНО; NULL — «без срока». Срок ничего не отключает сам:
    # к его концу суперадмин должен принять решение (продлить / снять срок / отключить). Мастеру срок не показывается.
    trial_ends_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Состояние ежедневных сводок суперадмину (чтобы не слать лишнего); любое действие над сроком сбрасывает оба поля в NULL.
    # Дата срока, о котором уже предупредили «заканчивается завтра» (одно предупреждение на срок).
    trial_expiring_notified_for: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Дата (по часовому поясу суперадмина) последней сводки, в которую мастер вошёл как «срок истёк».
    trial_expired_notified_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
