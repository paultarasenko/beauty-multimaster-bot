import datetime as dt

from sqlalchemy import delete, or_, select

from app.database.models import BlockedPeriod, BreakPeriod, WorkingDayOverride, WorkingHours
from app.database.repositories.base import MasterScopedRepository


class ScheduleRepository(MasterScopedRepository):

    async def get_working_hours(self, weekday: int) -> WorkingHours | None:
        result = await self.session.execute(
            select(WorkingHours).where(WorkingHours.master_id == self.master_id, WorkingHours.weekday == weekday)
        )
        return result.scalar_one_or_none()

    async def get_override(self, day: dt.date) -> WorkingDayOverride | None:
        """Исключение из расписания на дату (открытый или закрытый день) или None."""
        result = await self.session.execute(
            select(WorkingDayOverride).where(WorkingDayOverride.master_id == self.master_id, WorkingDayOverride.day == day)
        )
        return result.scalar_one_or_none()

    async def get_overrides_in_range(self, first_day: dt.date, last_day: dt.date) -> dict[dt.date, WorkingDayOverride]:
        """Исключения на даты от first_day до last_day включительно одним запросом: дата -> исключение."""
        result = await self.session.execute(
            select(WorkingDayOverride).where(
                WorkingDayOverride.master_id == self.master_id,
                WorkingDayOverride.day >= first_day,
                WorkingDayOverride.day <= last_day,
            )
        )
        return {row.day: row for row in result.scalars().all()}

    async def get_upcoming_overrides(self, from_day: dt.date, limit: int) -> list[WorkingDayOverride]:
        """Исключения на даты не раньше from_day — ближайшие первыми."""
        result = await self.session.execute(
            select(WorkingDayOverride)
            .where(WorkingDayOverride.master_id == self.master_id, WorkingDayOverride.day >= from_day)
            .order_by(WorkingDayOverride.day)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_working_hours_for_date(self, day: dt.date) -> WorkingHours | WorkingDayOverride | None:
        """Рабочие часы на конкретную дату: исключение на эту дату, а если его нет — расписание по дню недели."""
        override = await self.get_override(day)
        return override if override is not None else await self.get_working_hours(day.weekday())

    async def set_override(
        self, day: dt.date, is_working: bool, start_time: dt.time | None, end_time: dt.time | None
    ) -> WorkingDayOverride:
        """Создаёт или обновляет исключение на дату. Закрытый день хранится без времени."""
        if not is_working:
            start_time = end_time = None
        override = await self.get_override(day)
        if override is None:
            override = WorkingDayOverride(
                master_id=self.master_id, day=day, is_working=is_working, start_time=start_time, end_time=end_time
            )
            self.session.add(override)
        else:
            override.is_working = is_working
            override.start_time = start_time
            override.end_time = end_time
        await self.session.flush()
        return override

    async def delete_override(self, day: dt.date) -> bool:
        """Убирает исключение на дату (день снова живёт по расписанию дня недели). True, если оно было."""
        result = await self.session.execute(
            delete(WorkingDayOverride).where(WorkingDayOverride.master_id == self.master_id, WorkingDayOverride.day == day)
        )
        await self.session.flush()
        return result.rowcount > 0

    async def get_breaks(self, weekday: int) -> list[BreakPeriod]:
        result = await self.session.execute(
            select(BreakPeriod)
            .where(BreakPeriod.master_id == self.master_id, BreakPeriod.weekday == weekday)
            .order_by(BreakPeriod.start_time)
        )
        return list(result.scalars().all())

    async def get_all_working_hours(self) -> dict[int, WorkingHours]:
        """Рабочие часы всех дней недели одним запросом: weekday -> WorkingHours (пакетная загрузка, этап 12C)."""
        result = await self.session.execute(select(WorkingHours).where(WorkingHours.master_id == self.master_id))
        return {row.weekday: row for row in result.scalars().all()}

    async def get_all_breaks(self) -> dict[int, list[BreakPeriod]]:
        """Перерывы всех дней недели одним запросом: weekday -> перерывы по возрастанию начала (этап 12C)."""
        result = await self.session.execute(
            select(BreakPeriod)
            .where(BreakPeriod.master_id == self.master_id)
            .order_by(BreakPeriod.weekday, BreakPeriod.start_time)
        )
        grouped: dict[int, list[BreakPeriod]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.weekday, []).append(row)
        return grouped

    async def get_blocked_periods(
        self, range_start: dt.datetime, range_end: dt.datetime
    ) -> list[BlockedPeriod]:
        result = await self.session.execute(
            select(BlockedPeriod).where(
                BlockedPeriod.master_id == self.master_id,
                BlockedPeriod.start_datetime < range_end,
                BlockedPeriod.end_datetime > range_start,
            )
        )
        return list(result.scalars().all())

    async def get_upcoming_blocked_periods(self, now: dt.datetime, limit: int) -> list[BlockedPeriod]:
        """Блокировки, которые ещё не закончились, — ближайшие первыми."""
        result = await self.session.execute(
            select(BlockedPeriod)
            .where(BlockedPeriod.master_id == self.master_id, BlockedPeriod.end_datetime > now)
            .order_by(BlockedPeriod.start_datetime, BlockedPeriod.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_blocked_period(self, block_id: int) -> BlockedPeriod | None:
        result = await self.session.execute(
            select(BlockedPeriod).where(BlockedPeriod.master_id == self.master_id, BlockedPeriod.id == block_id)
        )
        return result.scalar_one_or_none()

    async def delete_blocked_period(self, blocked_period: BlockedPeriod) -> None:
        if blocked_period.master_id != self.master_id:
            raise ValueError("Блокировка принадлежит другому мастеру")
        await self.session.delete(blocked_period)
        await self.session.flush()

    async def upsert_working_hours(
        self, weekday: int, is_working: bool, start_time: dt.time | None, end_time: dt.time | None
    ) -> WorkingHours:
        wh = await self.get_working_hours(weekday)
        if wh is None:
            wh = WorkingHours(
                master_id=self.master_id,
                weekday=weekday,
                is_working=is_working,
                start_time=start_time,
                end_time=end_time,
            )
            self.session.add(wh)
        else:
            wh.is_working = is_working
            wh.start_time = start_time
            wh.end_time = end_time
        await self.session.flush()
        return wh

    async def add_break(self, weekday: int, start_time: dt.time, end_time: dt.time) -> BreakPeriod:
        br = BreakPeriod(master_id=self.master_id, weekday=weekday, start_time=start_time, end_time=end_time)
        self.session.add(br)
        await self.session.flush()
        return br

    async def set_lunch(self, weekday: int, start_time: dt.time, end_time: dt.time) -> BreakPeriod:
        """Единственный обеденный перерыв дня: прежние перерывы этого дня заменяются новым."""
        await self.clear_breaks(weekday)
        return await self.add_break(weekday, start_time, end_time)

    async def clear_breaks(self, weekday: int) -> int:
        """Удаляет все перерывы дня недели у этого мастера. Возвращает, сколько удалено."""
        result = await self.session.execute(
            delete(BreakPeriod).where(BreakPeriod.master_id == self.master_id, BreakPeriod.weekday == weekday)
        )
        await self.session.flush()
        return result.rowcount

    async def drop_breaks_outside_hours(self, weekday: int, start_time: dt.time, end_time: dt.time) -> int:
        """Удаляет перерывы дня, которые не помещаются целиком в рабочие часы [start_time, end_time].
        Нужна при смене часов работы: перерыв вне рабочего времени ничего не значит и только путает."""
        result = await self.session.execute(
            delete(BreakPeriod).where(
                BreakPeriod.master_id == self.master_id,
                BreakPeriod.weekday == weekday,
                or_(BreakPeriod.start_time < start_time, BreakPeriod.end_time > end_time),
            )
        )
        await self.session.flush()
        return result.rowcount

    async def add_blocked_period(
        self, start_datetime: dt.datetime, end_datetime: dt.datetime, reason: str | None
    ) -> BlockedPeriod:
        bp = BlockedPeriod(
            master_id=self.master_id, start_datetime=start_datetime, end_datetime=end_datetime, reason=reason
        )
        self.session.add(bp)
        await self.session.flush()
        return bp
