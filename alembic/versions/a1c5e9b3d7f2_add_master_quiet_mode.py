"""тихий режим мастера: master_quiet_mode (окно) и master_deferred_messages (отложенные сообщения)

Тихий режим — ежедневное окно (по часам мастера), в течение которого бот не присылает мастеру уведомления; накопленное
уходит после окончания окна. Существующие мастера не затронуты: строки создаются, когда мастер впервые открывает
«🌙 Тихий режим»; пока строки нет, режим выключен и поведение бота прежнее.

Revision ID: a1c5e9b3d7f2
Revises: a7f3b9c5d1e8
Create Date: 2026-10-23 01:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1c5e9b3d7f2'
down_revision: Union[str, None] = 'a7f3b9c5d1e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'master_quiet_mode',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('start_time', sa.Time(), server_default='22:00', nullable=False),
        sa.Column('end_time', sa.Time(), server_default='08:00', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['master_id'], ['masters.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('master_id', name='uq_master_quiet_mode_master_id'),
        sa.CheckConstraint('start_time <> end_time', name='ck_master_quiet_mode_window'),
    )
    op.create_table(
        'master_deferred_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('reply_markup', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['master_id'], ['masters.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_master_deferred_messages_master_id_id', 'master_deferred_messages', ['master_id', 'id'], unique=False
    )


def downgrade() -> None:
    # Внимание: откат удаляет настройки тихого режима и ещё не доставленные отложенные сообщения мастеров.
    op.drop_index('ix_master_deferred_messages_master_id_id', table_name='master_deferred_messages')
    op.drop_table('master_deferred_messages')
    op.drop_table('master_quiet_mode')
