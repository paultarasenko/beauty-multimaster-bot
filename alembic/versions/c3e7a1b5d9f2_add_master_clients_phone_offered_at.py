"""master_clients.phone_offered_at: когда клиенту предложили поделиться номером

Бот предлагает номер один раз, после первой записи. Отметка нужна, чтобы после «Не сейчас» больше не спрашивать
(передумать клиент может кнопкой в «Мои записи»). У существующих клиентов NULL: предложение они получат один раз,
после следующей записи, если номера у них ещё нет.

Revision ID: c3e7a1b5d9f2
Revises: b2d6f0a4c8e1
Create Date: 2026-11-01 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3e7a1b5d9f2'
down_revision: Union[str, None] = 'b2d6f0a4c8e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('master_clients', sa.Column('phone_offered_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('master_clients', 'phone_offered_at')
