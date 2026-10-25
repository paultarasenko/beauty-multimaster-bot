"""multi-tenant, этап 9: журнал действий суперадмина (superadmin_actions)

Служебная таблица владельца сервиса: кто, что, над каким мастером/ботом, когда и с каким результатом. Без внешних ключей
(запись журнала переживает мастера), без токенов и секретов.

Revision ID: w3b9c5d1e7f8
Revises: v2a8b4c0d6e7
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'w3b9c5d1e7f8'
down_revision: Union[str, None] = 'v2a8b4c0d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'superadmin_actions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('actor_telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target_master_id', sa.Integer(), nullable=True),
        sa.Column('target_bot_id', sa.BigInteger(), nullable=True),
        sa.Column('result', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("result IN ('ok', 'error')", name='ck_superadmin_actions_result'),
    )
    op.create_index('ix_superadmin_actions_created_at', 'superadmin_actions', ['created_at'], unique=False)


def downgrade() -> None:
    # Внимание: откат удаляет журнал действий суперадмина.
    op.drop_index('ix_superadmin_actions_created_at', table_name='superadmin_actions')
    op.drop_table('superadmin_actions')
