import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base


class ReferenceSnapshot(Base):
    """Эталонная база: копия данных мастера-эталона (мастера №1), которую суперадмин применяет к другим мастерам.

    Служебная таблица владельца сервиса, а не данные мастера: колонка называется `source_master_id` (не `master_id`) и
    намеренно БЕЗ внешнего ключа — эталон не должен пропасть вместе с мастером. Актуальный эталон — строка с наибольшим
    `id`; при каждой перезаписи вставляется новая строка, а прежние удаляются в той же транзакции (читатель, начавший
    чтение до перезаписи, продолжает видеть целую старую версию).

    `payload` — данные без фото (профиль, настройки, услуги, отзывы, портфолио, расписание), фото — в `ReferenceSnapshotPhoto`.
    Токенов, секретов, записей и клиентов здесь нет.
    """

    __tablename__ = "reference_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_master_id: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReferenceSnapshotPhoto(Base):
    """Файл эталона. Фото хранятся самими файлами, а не Telegram `file_id`: `file_id` действует только в том боте, который
    загрузил файл, а применять эталон нужно в ботах разных мастеров.

    `key`: `portfolio:<номер>` (фото портфолио), `profile` (фото в «Обо мне»), `bot_avatar` (аватарка бота).
    """

    __tablename__ = "reference_snapshot_photos"

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("reference_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
