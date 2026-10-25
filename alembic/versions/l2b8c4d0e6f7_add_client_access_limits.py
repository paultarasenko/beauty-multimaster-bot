"""users.is_restricted, users.cancel_streak_reset_at, bookings.cancelled_by_client (лимит записей и серия отмен)

Revision ID: l2b8c4d0e6f7
Revises: k1a7b3c9d5e6
Create Date: 2026-10-04 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'l2b8c4d0e6f7'
down_revision: Union[str, None] = 'k1a7b3c9d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('is_restricted', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('users', sa.Column('cancel_streak_reset_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('bookings', sa.Column('cancelled_by_client', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('bookings', 'cancelled_by_client')
    op.drop_column('users', 'cancel_streak_reset_at')
    op.drop_column('users', 'is_restricted')
