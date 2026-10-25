"""drop master_profile.faq_text

Revision ID: a8c3d5e7f1b4
Revises: f6b2c8d4a1e9
Create Date: 2026-09-24 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8c3d5e7f1b4'
down_revision: Union[str, None] = 'f6b2c8d4a1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Раздел «Вопросы и ответы» удалён из бота — текст FAQ больше не нужен.
    # IF EXISTS — чтобы миграция не падала, если колонку уже удалили вручную.
    op.execute("ALTER TABLE master_profile DROP COLUMN IF EXISTS faq_text")


def downgrade() -> None:
    # Возвращает пустую колонку; удалённый текст восстановить нельзя.
    op.execute("ALTER TABLE master_profile ADD COLUMN IF NOT EXISTS faq_text TEXT")
