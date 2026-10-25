"""add users.is_in_admin_panel

Revision ID: d3f6a9c1e5b8
Revises: c4e8a1d6b2f7
Create Date: 2026-09-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3f6a9c1e5b8'
down_revision: Union[str, None] = 'c4e8a1d6b2f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Режим меню админа (панель мастера / клиентский режим) переезжает из in-memory set()
    # (app/bot/menu_mode.py) в БД — раньше он терялся при каждом рестарте бота.
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_in_admin_panel BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_in_admin_panel")
