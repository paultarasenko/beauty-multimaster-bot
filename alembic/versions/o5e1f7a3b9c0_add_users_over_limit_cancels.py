"""users.over_limit_cancels (отмены клиента, у которого было больше трёх записей)

Revision ID: o5e1f7a3b9c0
Revises: n4d0e6f2a8b9
Create Date: 2026-10-04 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'o5e1f7a3b9c0'
down_revision: Union[str, None] = 'n4d0e6f2a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('over_limit_cancels', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'over_limit_cancels')
