"""add master_profile.faq_text

Revision ID: b7d1c2a4f9e3
Revises: 29037ac78cf7
Create Date: 2026-09-20 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7d1c2a4f9e3'
down_revision: Union[str, None] = '29037ac78cf7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Текст раздела «Вопросы и ответы», который мастер редактирует в боте (одним сообщением).
    # IF NOT EXISTS — чтобы миграция не падала, если колонку уже добавили вручную.
    op.execute("ALTER TABLE master_profile ADD COLUMN IF NOT EXISTS faq_text TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE master_profile DROP COLUMN IF EXISTS faq_text")
