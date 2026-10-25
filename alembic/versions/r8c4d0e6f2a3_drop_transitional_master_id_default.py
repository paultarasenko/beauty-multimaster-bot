"""multi-tenant, этап 2: убираем переходный server_default у master_id

Код приложения теперь всегда передаёт master_id явно (TenantContext → репозитории/сервисы). Без default
вставка строки без master_id падает на NOT NULL, а не молча попадает мастеру №1.

Revision ID: r8c4d0e6f2a3
Revises: q7b3c9d5e1f2
Create Date: 2026-10-07 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'r8c4d0e6f2a3'
down_revision: Union[str, None] = 'q7b3c9d5e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Все таблицы с master_id (список этапа 1 + master_clients).
TENANT_TABLES: list[str] = [
    'services',
    'working_hours',
    'breaks',
    'blocked_periods',
    'bookings',
    'reviews',
    'portfolio_items',
    'notifications',
    'admin_reminders',
    'client_access_requests',
    'booking_limit_requests',
    'master_profile',
    'master_clients',
]


def upgrade() -> None:
    for table in TENANT_TABLES:
        op.alter_column(table, 'master_id', server_default=None)


def downgrade() -> None:
    # Возвращает переходный default этапа 1 (мастер №1).
    for table in TENANT_TABLES:
        op.alter_column(table, 'master_id', server_default='1')
