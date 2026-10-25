"""multi-tenant, этап 4: таблица master_bots (свой Telegram-бот у каждого мастера)

Один бот на мастера (UNIQUE master_id), один мастер на бота (UNIQUE telegram_bot_id). Токен — только в
зашифрованном виде. Данные не переносятся: бот мастера №1 подключается командой `python -m app.cli add-bot`.

Revision ID: t0e6f2a8b4c5
Revises: s9d5e1f7a3b4
Create Date: 2026-10-07 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 't0e6f2a8b4c5'
down_revision: Union[str, None] = 's9d5e1f7a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'master_bots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('telegram_bot_id', sa.BigInteger(), nullable=False),
        sa.Column('bot_username', sa.String(length=64), nullable=True),
        sa.Column('bot_token_encrypted', sa.Text(), nullable=False),
        sa.Column('webhook_secret', sa.String(length=128), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['master_id'], ['masters.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('master_id'),
        sa.UniqueConstraint('telegram_bot_id'),
        sa.UniqueConstraint('webhook_secret'),
    )


def downgrade() -> None:
    # Внимание: откат удаляет подключённых ботов (токены и webhook-секреты). У Telegram webhook при этом остаётся
    # установленным — перед откатом выполните `python -m app.cli remove-bot` для каждого мастера.
    op.drop_table('master_bots')
