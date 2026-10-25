"""эталонная база в БД: reference_snapshots и reference_snapshot_photos

Эталон (данные мастера №1, которые суперадмин применяет кнопкой «Обновить базу» к другим мастерам) раньше был зашит в код
(app/reference/data.py). Теперь он хранится в БД и обновляется кнопкой «Перезаписать базу» у мастера №1. Служебные таблицы
владельца сервиса: без master_id и без внешнего ключа на masters. Существующие данные не меняются; пока эталон не перезаписан
ни разу, строк в таблицах нет и «Обновить базу» просит сначала перезаписать эталон.

Revision ID: z6e2f8a4b0c1
Revises: y5d1e7f3a9b0
Create Date: 2026-10-19 19:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'z6e2f8a4b0c1'
down_revision: Union[str, None] = 'y5d1e7f3a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reference_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_master_id', sa.Integer(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_by_telegram_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'reference_snapshot_photos',
        sa.Column('snapshot_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=32), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(['snapshot_id'], ['reference_snapshots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('snapshot_id', 'key'),
    )


def downgrade() -> None:
    # Внимание: откат удаляет сохранённый эталон (его можно создать заново кнопкой «Перезаписать базу» у мастера №1).
    op.drop_table('reference_snapshot_photos')
    op.drop_table('reference_snapshots')
