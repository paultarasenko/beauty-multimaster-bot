import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, TimestampMixin


class MasterClient(Base, TimestampMixin):
    """Связь «клиент мастера»: один и тот же Telegram-пользователь (`users`) может быть клиентом нескольких мастеров.

    `users` — глобальная личность по telegram_id, а всё, что относится к отношениям с конкретным мастером,
    живёт здесь: ограничения доступа, серия отмен, состояние «сверх лимита». У одного человека у разных
    мастеров эти значения независимы.
    """

    __tablename__ = "master_clients"
    __table_args__ = (UniqueConstraint("master_id", "user_id", name="uq_master_clients_master_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Мастер ограничил клиенту доступ: новые записи недоступны (уже созданные остаются).
    is_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Мастер нажал «Ограничить навсегда»: клиент не может ни записываться, ни писать мастеру, ни оставлять отзывы.
    is_blocked_forever: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Серия отмен считается только по записям, созданным после этого момента: мастер нажал «Подтвердить доступ»,
    # и клиент начинает «с чистого листа», а не получает то же предупреждение при следующей отмене.
    cancel_streak_reset_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Клиент, у которого стало больше трёх записей (мастер дал разрешение сверх лимита и клиент им воспользовался),
    # «в состоянии сверх лимита»: считаем его отмены, любые, не обязательно подряд. NULL — клиент в этом состоянии
    # не находится. Три отмены — предупреждение мастеру; возврат в обычный лимит, решение мастера или предупреждение
    # обнуляют (NULL). Это точная отметка, а не вывод из «использованных разрешений»: сгоревшее разрешение её не ставит.
    over_limit_cancels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Владелец этого мастера сейчас в админ-панели (а не в клиентском меню своего же бота): флаг нужен, потому что кнопки
    # «ℹ️ Информация», «⬅️ Назад» и т.п. одинаковы в обоих меню. Хранится у связи с мастером, поэтому человек, который
    # владелец мастера A и клиент мастера B, имеет независимые режимы в двух ботах.
    is_in_admin_panel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Номер телефона клиента, подтверждённый Telegram (кнопка «Поделиться номером»). Хранится у связи с мастером,
    # а не в users: номер, оставленный одному мастеру, не должен быть виден другому. NULL — номер ещё не запрашивали.
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Когда боту уже предлагали поделиться номером (после первой записи). Не NULL — сами больше не спрашиваем:
    # «Не сейчас» или молчание считаем отказом. Передумать клиент может сам, кнопкой в «Мои записи».
    phone_offered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
