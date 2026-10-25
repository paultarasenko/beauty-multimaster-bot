"""add services.buffer_minutes and bookings.service_buffer_snapshot (свободное время после услуги)

Revision ID: k1a7b3c9d5e6
Revises: j0f6a2b8e4d5
Create Date: 2026-10-04 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'k1a7b3c9d5e6'
down_revision: Union[str, None] = 'j0f6a2b8e4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('services', sa.Column('buffer_minutes', sa.Integer(), server_default='0', nullable=False))
    op.create_check_constraint('ck_services_buffer_non_negative', 'services', 'buffer_minutes >= 0')
    op.add_column('bookings', sa.Column('service_buffer_snapshot', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('bookings', 'service_buffer_snapshot')
    op.drop_constraint('ck_services_buffer_non_negative', 'services', type_='check')
    op.drop_column('services', 'buffer_minutes')
