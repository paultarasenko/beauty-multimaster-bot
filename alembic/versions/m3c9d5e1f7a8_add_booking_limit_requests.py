"""add booking_limit_requests (запросы клиентов к мастеру на запись сверх лимита)

Revision ID: m3c9d5e1f7a8
Revises: l2b8c4d0e6f7
Create Date: 2026-10-04 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'm3c9d5e1f7a8'
down_revision: Union[str, None] = 'l2b8c4d0e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'booking_limit_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('master_comment', sa.Text(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('client_notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('override_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name='ck_booking_limit_requests_status',
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_booking_limit_requests_user_id'), 'booking_limit_requests', ['user_id'], unique=False)
    op.create_index(
        'uq_booking_limit_requests_one_pending',
        'booking_limit_requests',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index('uq_booking_limit_requests_one_pending', table_name='booking_limit_requests')
    op.drop_index(op.f('ix_booking_limit_requests_user_id'), table_name='booking_limit_requests')
    op.drop_table('booking_limit_requests')
