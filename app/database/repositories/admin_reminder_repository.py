import datetime as dt

from sqlalchemy import delete, func, select, update

from app.database.models import AdminReminder
from app.database.repositories.base import MasterScopedRepository


class AdminReminderRepository(MasterScopedRepository):

    async def add(
        self,
        text: str,
        recurrence: str,
        anchor_date: dt.date,
        remind_time: dt.time,
        next_fire_at: dt.datetime,
    ) -> AdminReminder:
        reminder = AdminReminder(
            master_id=self.master_id,
            text=text,
            recurrence=recurrence,
            anchor_date=anchor_date,
            remind_time=remind_time,
            next_fire_at=next_fire_at,
        )
        self.session.add(reminder)
        await self.session.flush()
        return reminder

    async def get(self, reminder_id: int) -> AdminReminder | None:
        result = await self.session.execute(
            select(AdminReminder).where(AdminReminder.master_id == self.master_id, AdminReminder.id == reminder_id)
        )
        return result.scalar_one_or_none()

    async def list_upcoming(self, limit: int) -> list[AdminReminder]:
        """Ближайшие по времени срабатывания — первыми."""
        result = await self.session.execute(
            select(AdminReminder)
            .where(AdminReminder.master_id == self.master_id)
            .order_by(AdminReminder.next_fire_at, AdminReminder.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count(self) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(AdminReminder).where(AdminReminder.master_id == self.master_id)
        )
        return result.scalar_one()

    async def get_due(self, now: dt.datetime) -> list[AdminReminder]:
        result = await self.session.execute(
            select(AdminReminder)
            .where(AdminReminder.master_id == self.master_id, AdminReminder.next_fire_at <= now)
            .order_by(AdminReminder.next_fire_at)
        )
        return list(result.scalars().all())

    async def delete(self, reminder_id: int) -> bool:
        result = await self.session.execute(
            delete(AdminReminder).where(AdminReminder.master_id == self.master_id, AdminReminder.id == reminder_id)
        )
        return result.rowcount > 0

    async def advance(self, reminder_id: int, fired_at: dt.datetime, next_fire_at: dt.datetime) -> bool:
        """Переносит повторяющееся напоминание на следующее срабатывание.

        Условие `next_fire_at == fired_at` — compare-and-set: если напоминание уже забрал другой воркер
        или мастер его удалил/изменил, строк не найдётся и False вернётся — отправлять нельзя.
        """
        result = await self.session.execute(
            update(AdminReminder)
            .where(
                AdminReminder.master_id == self.master_id,
                AdminReminder.id == reminder_id,
                AdminReminder.next_fire_at == fired_at,
            )
            .values(next_fire_at=next_fire_at)
        )
        return result.rowcount > 0

    async def delete_if_fired_at(self, reminder_id: int, fired_at: dt.datetime) -> bool:
        """Удаляет одноразовое напоминание после срабатывания (та же compare-and-set защита, что в advance)."""
        result = await self.session.execute(
            delete(AdminReminder).where(
                AdminReminder.master_id == self.master_id,
                AdminReminder.id == reminder_id,
                AdminReminder.next_fire_at == fired_at,
            )
        )
        return result.rowcount > 0
