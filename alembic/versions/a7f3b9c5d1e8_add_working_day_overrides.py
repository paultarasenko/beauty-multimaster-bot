"""исключения из расписания на дату: working_day_overrides

Позволяют открыть одну дату, когда по расписанию дня недели мастер не работает (например, одну субботу), или закрыть одну
дату целиком (отпуск, праздник). Строка на дату главнее расписания по дням недели (working_hours). Существующие данные не
меняются: пока строк нет, расписание работает как раньше.

Revision ID: a7f3b9c5d1e8
Revises: z6e2f8a4b0c1
Create Date: 2026-10-22 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7f3b9c5d1e8'
down_revision: Union[str, None] = 'z6e2f8a4b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'working_day_overrides',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('is_working', sa.Boolean(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=True),
        sa.Column('end_time', sa.Time(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['master_id'], ['masters.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('master_id', 'day', name='uq_working_day_overrides_master_day'),
        sa.CheckConstraint(
            'is_working = false OR (start_time IS NOT NULL AND end_time IS NOT NULL AND start_time < end_time)',
            name='ck_working_day_overrides_hours',
        ),
    )


def downgrade() -> None:
    # Внимание: откат удаляет все исключения на даты; расписание по дням недели остаётся.
    op.drop_table('working_day_overrides')
