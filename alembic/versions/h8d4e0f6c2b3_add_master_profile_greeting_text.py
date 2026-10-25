"""add master_profile.greeting_text (своё приветствие мастера; NULL — стандартное)

Revision ID: h8d4e0f6c2b3
Revises: g7c3d9e5b1a2
Create Date: 2026-10-01 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'h8d4e0f6c2b3'
down_revision: Union[str, None] = 'g7c3d9e5b1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('master_profile', sa.Column('greeting_text', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('master_profile', 'greeting_text')
