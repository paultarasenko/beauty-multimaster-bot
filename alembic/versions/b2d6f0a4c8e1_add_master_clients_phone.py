"""телефон клиента у мастера: master_clients.phone

Номер подтверждается Telegram (клиент делится контактом своего аккаунта) и запрашивается один раз, при первой записи.
Хранится у связи «клиент мастера», а не в users, чтобы номер не был виден другим мастерам. У существующих клиентов
значение NULL: бот попросит номер при их следующей записи.

Revision ID: b2d6f0a4c8e1
Revises: a1c5e9b3d7f2
Create Date: 2026-11-01 01:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2d6f0a4c8e1'
down_revision: Union[str, None] = 'a1c5e9b3d7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('master_clients', sa.Column('phone', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('master_clients', 'phone')
