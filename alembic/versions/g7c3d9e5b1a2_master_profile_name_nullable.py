"""master_profile.name nullable (мастер может удалить имя)

Revision ID: g7c3d9e5b1a2
Revises: a8c3d5e7f1b4
Create Date: 2026-09-28 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'g7c3d9e5b1a2'
down_revision: Union[str, None] = 'a8c3d5e7f1b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Кнопка «Имя» → «Удалить» очищает имя, поэтому NULL должен быть допустим.
    op.execute("ALTER TABLE master_profile ALTER COLUMN name DROP NOT NULL")


def downgrade() -> None:
    # Пустые имена заполняем, иначе NOT NULL не применится.
    op.execute("UPDATE master_profile SET name = '' WHERE name IS NULL")
    op.execute("ALTER TABLE master_profile ALTER COLUMN name SET NOT NULL")
