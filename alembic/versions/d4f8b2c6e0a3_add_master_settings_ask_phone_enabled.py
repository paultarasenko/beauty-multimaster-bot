"""master_settings.ask_phone_enabled: мастер включает запрос сотового номера у клиентов

По умолчанию выключено, поэтому у всех существующих мастеров после обновления поведение бота не меняется,
пока мастер сам не включит «📱 Запрос сотового номера» в «⚙️ Настройки».

Revision ID: d4f8b2c6e0a3
Revises: c3e7a1b5d9f2
Create Date: 2026-11-02 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4f8b2c6e0a3'
down_revision: Union[str, None] = 'c3e7a1b5d9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'master_settings',
        sa.Column('ask_phone_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('master_settings', 'ask_phone_enabled')
