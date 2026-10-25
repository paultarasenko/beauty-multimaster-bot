"""add admin_reminders (личные напоминания мастера: один раз / день / неделя / месяц / год)

Revision ID: j0f6a2b8e4d5
Revises: i9e5f1a7d3c4
Create Date: 2026-10-02 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'j0f6a2b8e4d5'
down_revision: Union[str, None] = 'i9e5f1a7d3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'admin_reminders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('recurrence', sa.String(length=20), nullable=False),
        sa.Column('anchor_date', sa.Date(), nullable=False),
        sa.Column('remind_time', sa.Time(), nullable=False),
        sa.Column('next_fire_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "recurrence IN ('once', 'daily', 'weekly', 'monthly', 'yearly')",
            name='ck_admin_reminders_recurrence',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_admin_reminders_next_fire_at'), 'admin_reminders', ['next_fire_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_admin_reminders_next_fire_at'), table_name='admin_reminders')
    op.drop_table('admin_reminders')
