from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.models import MasterSettings
from app.database.repositories.base import MasterScopedRepository
from app.utils.master_settings import SETTING_FIELDS, MasterSettingsData, default_value, settings_from_row


class MasterSettingsRepository(MasterScopedRepository):
    """Настройки ОДНОГО мастера (этап 8). Все запросы фильтруются по master_id."""

    async def get_row(self) -> MasterSettings | None:
        result = await self.session.execute(select(MasterSettings).where(MasterSettings.master_id == self.master_id))
        return result.scalar_one_or_none()

    async def get_data(self) -> MasterSettingsData:
        """Снимок настроек. Если строки ещё нет — значения по умолчанию (прежнее поведение бота); ничего не пишет."""
        row = await self.get_row()
        return settings_from_row(row) if row is not None else MasterSettingsData()

    async def ensure_row(self) -> MasterSettings:
        """Строка настроек мастера; создаётся со значениями по умолчанию, если её нет (безопасно при гонке)."""
        row = await self.get_row()
        if row is not None:
            return row
        await self.session.execute(
            pg_insert(MasterSettings).values(master_id=self.master_id).on_conflict_do_nothing(index_elements=["master_id"])
        )
        row = await self.get_row()
        assert row is not None
        return row

    async def update_fields(self, **values) -> MasterSettings:
        """Меняет перечисленные настройки (значения уже проверены вызывающим) и возвращает строку."""
        unknown = set(values) - set(SETTING_FIELDS)
        if unknown:
            raise KeyError(f"Неизвестные настройки: {sorted(unknown)}")
        row = await self.ensure_row()
        for name, value in values.items():
            setattr(row, name, value)
        await self.session.flush()
        return row

    async def reset_fields(self, fields: list[str] | None = None) -> MasterSettings:
        """Сбрасывает настройки к значениям по умолчанию (все, если список не задан)."""
        names = list(SETTING_FIELDS) if fields is None else fields
        return await self.update_fields(**{name: default_value(name) for name in names})
