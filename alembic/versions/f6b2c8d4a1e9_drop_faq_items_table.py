"""drop faq_items table

Revision ID: f6b2c8d4a1e9
Revises: e5a1f7c9b3d2
Create Date: 2026-09-24 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f6b2c8d4a1e9'
down_revision: Union[str, None] = 'e5a1f7c9b3d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Таблица с отдельными пунктами FAQ нигде не использовалась (FAQ хранится одним
    # текстом в master_profile.faq_text), поэтому просто удаляем её вместе с моделью.
    op.execute("DROP TABLE IF EXISTS faq_items")


def downgrade() -> None:
    op.create_table('faq_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('question', sa.String(length=500), nullable=False),
    sa.Column('answer', sa.Text(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
