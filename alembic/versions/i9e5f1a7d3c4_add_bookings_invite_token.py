"""add bookings.invite_token (одноразовая ссылка-приглашение клиента в бота)

Revision ID: i9e5f1a7d3c4
Revises: h8d4e0f6c2b3
Create Date: 2026-10-01 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'i9e5f1a7d3c4'
down_revision: Union[str, None] = 'h8d4e0f6c2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('invite_token', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_bookings_invite_token'), 'bookings', ['invite_token'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_bookings_invite_token'), table_name='bookings')
    op.drop_column('bookings', 'invite_token')
