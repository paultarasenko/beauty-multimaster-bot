"""users.is_blocked_forever + client_access_requests (обращения ограниченных клиентов к мастеру)

Revision ID: n4d0e6f2a8b9
Revises: m3c9d5e1f7a8
Create Date: 2026-10-04 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'n4d0e6f2a8b9'
down_revision: Union[str, None] = 'm3c9d5e1f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('is_blocked_forever', sa.Boolean(), server_default='false', nullable=False))
    op.create_table(
        'client_access_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'opened', 'blocked')",
            name='ck_client_access_requests_status',
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_client_access_requests_user_id'), 'client_access_requests', ['user_id'], unique=False)
    op.create_index(
        'uq_client_access_requests_one_pending',
        'client_access_requests',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index('uq_client_access_requests_one_pending', table_name='client_access_requests')
    op.drop_index(op.f('ix_client_access_requests_user_id'), table_name='client_access_requests')
    op.drop_table('client_access_requests')
    op.drop_column('users', 'is_blocked_forever')
