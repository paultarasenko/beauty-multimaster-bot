"""Общая основа репозиториев данных мастера: без корректного `master_id` репозиторий не создаётся (этап 5)."""

from sqlalchemy.ext.asyncio import AsyncSession


class MasterIdRequiredError(ValueError):
    """Репозиторий создан без мастера (или с некорректным master_id): запросы остались бы без фильтра изоляции."""


class MasterScopedRepository:
    """Базовый класс: хранит `session` и `master_id`, по которому репозиторий фильтрует ВСЕ запросы.

    `master_id` обязателен и не имеет значения по умолчанию. `None`, bool, не целое и неположительное значение —
    ошибка при создании: молчаливый `master_id IS NULL` или `0` не должен превращаться в «пустой» результат, который
    маскирует баг (забыли передать мастера). Сторож `tests/test_stage5_guards.py` проверяет, что все репозитории
    пакета наследуются отсюда.
    """

    def __init__(self, session: AsyncSession, master_id: int):
        if isinstance(master_id, bool) or not isinstance(master_id, int) or master_id <= 0:
            raise MasterIdRequiredError(f"{type(self).__name__}: нужен master_id (положительное целое), получено {master_id!r}")
        self.session = session
        self.master_id = master_id
