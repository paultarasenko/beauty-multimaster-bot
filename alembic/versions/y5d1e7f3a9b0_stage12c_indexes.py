"""этап 12C: индексы по результатам EXPLAIN (клиентские запросы броней, проверка занятости, зависшие уведомления)

Три индекса (цифры — в ARCHITECTURE.md, «Этап 12C»):
- bookings (master_id, user_id, start_datetime): записи клиента у мастера;
- bookings (master_id, end_datetime) WHERE status = 'confirmed': проверка занятости и завершение прошедших записей;
- notifications (master_id, claimed_at) WHERE status = 'sending' — вместо ix_notifications_sending_claimed (claimed_at):
  запросы мастера идут по индексу с master_id первым столбцом.
Данные не меняются, откат возвращает прежний набор индексов.

Revision ID: y5d1e7f3a9b0
Revises: x4c0d6e2f8a9
Create Date: 2026-10-13 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'y5d1e7f3a9b0'
down_revision: Union[str, None] = 'x4c0d6e2f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_bookings_master_user_start', 'bookings', ['master_id', 'user_id', 'start_datetime'])
    op.create_index(
        'ix_bookings_master_end_confirmed',
        'bookings',
        ['master_id', 'end_datetime'],
        postgresql_where=sa.text("status = 'confirmed'"),
    )
    op.create_index(
        'ix_notifications_master_sending',
        'notifications',
        ['master_id', 'claimed_at'],
        postgresql_where=sa.text("status = 'sending'"),
    )
    op.drop_index(
        'ix_notifications_sending_claimed', table_name='notifications', postgresql_where=sa.text("status = 'sending'")
    )


def downgrade() -> None:
    op.create_index(
        'ix_notifications_sending_claimed',
        'notifications',
        ['claimed_at'],
        postgresql_where=sa.text("status = 'sending'"),
    )
    op.drop_index(
        'ix_notifications_master_sending', table_name='notifications', postgresql_where=sa.text("status = 'sending'")
    )
    op.drop_index(
        'ix_bookings_master_end_confirmed', table_name='bookings', postgresql_where=sa.text("status = 'confirmed'")
    )
    op.drop_index('ix_bookings_master_user_start', table_name='bookings')
