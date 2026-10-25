from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Master
from app.utils.master_settings import MasterSettingsData


@dataclass(frozen=True)
class TenantContext:
    """Мастер, от имени которого обрабатывается апдейт (арендатор системы).

    Кладётся в data апдейта middleware'ом (ключ `tenant`); хендлеры берут `tenant.master_id` и передают его
    в репозитории и сервисы. `master` — ORM-объект из сессии этого апдейта.

    Настройки мастера (этап 8) — `await tenant.get_settings()`: читаются из БД лениво, один раз за апдейт
    (контекст живёт один апдейт), без кэша на уровне процесса. После изменения настроек в этом же апдейте нужен
    `invalidate_settings()`.
    """

    master_id: int
    master: Master
    session: AsyncSession | None = field(default=None, repr=False, compare=False)
    _cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    async def get_settings(self) -> MasterSettingsData:
        cached = self._cache.get("settings")
        if cached is None:
            if self.session is None:
                raise RuntimeError("TenantContext без сессии БД: настройки мастера прочитать нельзя")
            from app.services.master_settings_service import load_master_settings

            cached = await load_master_settings(self.session, self.master_id)
            self._cache["settings"] = cached
        return cached

    def invalidate_settings(self) -> None:
        """Забывает загруженные настройки (после их изменения в этом же апдейте)."""
        self._cache.pop("settings", None)
