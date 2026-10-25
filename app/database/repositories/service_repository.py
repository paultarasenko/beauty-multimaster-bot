from sqlalchemy import select

from app.database.models import Service
from app.database.repositories.base import MasterScopedRepository


class ServiceRepository(MasterScopedRepository):

    async def get_active(self) -> list[Service]:
        result = await self.session.execute(
            select(Service)
            .where(Service.master_id == self.master_id, Service.active.is_(True))
            .order_by(Service.sort_order, Service.id)
        )
        return list(result.scalars().all())

    async def get_all(self) -> list[Service]:
        result = await self.session.execute(
            select(Service).where(Service.master_id == self.master_id).order_by(Service.sort_order, Service.id)
        )
        return list(result.scalars().all())

    async def get_by_id(self, service_id: int) -> Service | None:
        """Услуга только своего мастера: id чужой услуги (из подделанной callback_data) даёт None."""
        result = await self.session.execute(
            select(Service).where(Service.master_id == self.master_id, Service.id == service_id)
        )
        return result.scalar_one_or_none()
