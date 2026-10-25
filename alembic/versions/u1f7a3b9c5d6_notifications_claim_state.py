"""multi-tenant, этап 6: «захват» уведомлений планировщиком (статус sending + claimed_at) и индекс выборки

Планировщик забирает созревшие уведомления атомарно (`FOR UPDATE SKIP LOCKED`): pending -> sending -> sent/failed.
`claimed_at` — когда уведомление взято в работу: «зависшие» в sending (процесс упал посреди отправки) возвращаются
в pending по таймауту. Частичный индекс ускоряет выборку «созревших» по мастеру и времени. Статус — обычная строка
(String(20)) без CHECK-ограничения, поэтому новое значение `sending` схему не меняет.

Revision ID: u1f7a3b9c5d6
Revises: t0e6f2a8b4c5
Create Date: 2026-10-08 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'u1f7a3b9c5d6'
down_revision: Union[str, None] = 't0e6f2a8b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        'ix_notifications_master_due',
        'notifications',
        ['master_id', 'scheduled_at'],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        'ix_notifications_sending_claimed',
        'notifications',
        ['claimed_at'],
        unique=False,
        postgresql_where=sa.text("status = 'sending'"),
    )


def downgrade() -> None:
    # Уведомления, которые на момент отката были в sending, возвращаем в pending: старый код статуса sending не знает.
    op.execute("UPDATE notifications SET status = 'pending' WHERE status = 'sending'")
    op.drop_index('ix_notifications_sending_claimed', table_name='notifications', postgresql_where=sa.text("status = 'sending'"))
    op.drop_index('ix_notifications_master_due', table_name='notifications', postgresql_where=sa.text("status = 'pending'"))
    op.drop_column('notifications', 'claimed_at')
