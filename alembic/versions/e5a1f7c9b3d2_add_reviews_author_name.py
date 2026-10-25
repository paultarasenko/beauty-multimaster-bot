"""add reviews.author_name

Revision ID: e5a1f7c9b3d2
Revises: d3f6a9c1e5b8
Create Date: 2026-09-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5a1f7c9b3d2'
down_revision: Union[str, None] = 'd3f6a9c1e5b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Имя автора отзыва клиент теперь вводит сам (не берём его из профиля Telegram) —
    # это отдельное текстовое поле, которого раньше в таблице не было.
    op.execute(
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS author_name VARCHAR(100)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reviews DROP COLUMN IF EXISTS author_name")
