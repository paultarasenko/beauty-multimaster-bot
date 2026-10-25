import datetime as dt

import pytest

from app.bot.handlers.admin import schedule
from app.database.repositories.schedule_repository import ScheduleRepository
from tests.conftest import MASTER_ID

TZ = dt.timezone(dt.timedelta(hours=2))


def test_day_time_grid_starts_at_day_start_with_configured_step():
    grid = schedule._day_time_grid(TZ, 30)

    assert grid[0].time() == schedule.DAY_START == dt.time(5, 0)
    assert all(t.tzinfo is not None for t in grid)
    deltas = {b - a for a, b in zip(grid, grid[1:])}
    assert deltas == {dt.timedelta(minutes=30)}


def test_day_time_grid_last_slot_never_exceeds_day_end():
    """Сетка не должна выходить за DAY_END — иначе на последнем времени начала
    не останется доступного времени окончания (см. select_start_time)."""
    grid = schedule._day_time_grid(TZ, 45)

    assert grid[-1].time() <= schedule.DAY_END
    next_point = grid[-1] + dt.timedelta(minutes=45)
    # Следующая точка либо перепрыгивает через полночь, либо уходит за DAY_END того же дня —
    # в обоих случаях она законно исключена из сетки.
    assert next_point.date() != grid[-1].date() or next_point.time() > schedule.DAY_END


def test_day_time_grid_default_step_reaches_day_end():
    """При стандартном шаге 30 минут сетка от DAY_START доходит ровно до DAY_END (23:00)."""
    grid = schedule._day_time_grid(TZ, 30)

    assert grid[-1].time() == schedule.DAY_END == dt.time(23, 0)


@pytest.mark.asyncio
async def test_set_working_hours_via_repository(session):
    """То же действие, что делает confirm_schedule после выбора времени кнопками."""
    repo = ScheduleRepository(session, MASTER_ID)
    weekday = 0  # понедельник

    await repo.upsert_working_hours(weekday, is_working=True, start_time=dt.time(9, 0), end_time=dt.time(18, 0))
    await session.commit()

    wh = await repo.get_working_hours(weekday)
    assert wh.is_working is True
    assert wh.start_time == dt.time(9, 0)
    assert wh.end_time == dt.time(18, 0)


@pytest.mark.asyncio
async def test_set_day_off_via_repository_clears_previous_hours(session):
    """То же действие, что делает set_day_off после нажатия «Сделать выходным»:
    ранее сохранённые рабочие часы должны быть сброшены, а не просто помечены неактивными."""
    repo = ScheduleRepository(session, MASTER_ID)
    weekday = 1  # вторник

    await repo.upsert_working_hours(weekday, is_working=True, start_time=dt.time(10, 0), end_time=dt.time(19, 0))
    await session.commit()

    await repo.upsert_working_hours(weekday, is_working=False, start_time=None, end_time=None)
    await session.commit()

    wh = await repo.get_working_hours(weekday)
    assert wh.is_working is False
    assert wh.start_time is None
    assert wh.end_time is None


@pytest.mark.asyncio
async def test_reselecting_working_day_after_day_off_restores_hours(session):
    """Мастер сначала сделал день выходным, потом передумал и задал часы — новый флоу
    позволяет вернуться на день назад и выбрать другое время, это должно перезаписывать данные."""
    repo = ScheduleRepository(session, MASTER_ID)
    weekday = 2  # среда

    await repo.upsert_working_hours(weekday, is_working=False, start_time=None, end_time=None)
    await session.commit()

    await repo.upsert_working_hours(weekday, is_working=True, start_time=dt.time(11, 0), end_time=dt.time(20, 0))
    await session.commit()

    wh = await repo.get_working_hours(weekday)
    assert wh.is_working is True
    assert wh.start_time == dt.time(11, 0)
    assert wh.end_time == dt.time(20, 0)