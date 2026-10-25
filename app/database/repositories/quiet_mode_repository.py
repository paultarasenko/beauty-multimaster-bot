import datetime as dt

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.models import MasterDeferredMessage, MasterQuietMode
from app.database.repositories.base import MasterScopedRepository
from app.utils.quiet_mode import QuietModeData


class QuietModeRepository(MasterScopedRepository):
    """Тихий режим ОДНОГО мастера. Все запросы фильтруются по master_id."""

    async def get_row(self) -> MasterQuietMode | None:
        result = await self.session.execute(select(MasterQuietMode).where(MasterQuietMode.master_id == self.master_id))
        return result.scalar_one_or_none()

    async def get_data(self) -> QuietModeData:
        """Снимок. Строки нет — режим выключен (прежнее поведение бота); ничего не пишет."""
        row = await self.get_row()
        if row is None:
            return QuietModeData()
        return QuietModeData(enabled=row.enabled, start_time=row.start_time, end_time=row.end_time)

    async def save(self, *, enabled: bool, start_time: dt.time, end_time: dt.time) -> None:
        """Создаёт или обновляет строку (безопасно при гонке). Значения уже проверены вызывающим."""
        statement = pg_insert(MasterQuietMode).values(
            master_id=self.master_id, enabled=enabled, start_time=start_time, end_time=end_time
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["master_id"],
                set_={
                    "enabled": statement.excluded.enabled,
                    "start_time": statement.excluded.start_time,
                    "end_time": statement.excluded.end_time,
                    "updated_at": func.now(),
                },
            )
        )


class DeferredMessageRepository(MasterScopedRepository):
    """Сообщения мастеру, отложенные тихим режимом."""

    async def add(
        self, text: str, reply_markup: dict | None = None, expires_at: dt.datetime | None = None
    ) -> MasterDeferredMessage:
        message = MasterDeferredMessage(
            master_id=self.master_id, text=text, reply_markup=reply_markup, expires_at=expires_at
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def count(self) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(MasterDeferredMessage)
            .where(MasterDeferredMessage.master_id == self.master_id)
        )
        return result.scalar_one()

    async def delete_expired(self, now: dt.datetime) -> int:
        result = await self.session.execute(
            delete(MasterDeferredMessage).where(
                MasterDeferredMessage.master_id == self.master_id,
                MasterDeferredMessage.expires_at.is_not(None),
                MasterDeferredMessage.expires_at <= now,
            )
        )
        return result.rowcount

    async def claim_oldest(self) -> MasterDeferredMessage | None:
        """Самое старое сообщение под блокировкой строки: параллельный воркер его пропустит (SKIP LOCKED)."""
        result = await self.session.execute(
            select(MasterDeferredMessage)
            .where(MasterDeferredMessage.master_id == self.master_id)
            .order_by(MasterDeferredMessage.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return result.scalar_one_or_none()

    async def delete(self, message_id: int) -> None:
        await self.session.execute(
            delete(MasterDeferredMessage).where(
                MasterDeferredMessage.master_id == self.master_id, MasterDeferredMessage.id == message_id
            )
        )
