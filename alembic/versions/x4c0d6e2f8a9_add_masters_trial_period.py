"""multi-tenant, этап 10: пробный период мастера (masters.trial_ends_on и состояние уведомлений)

Срок — последний день включительно; NULL = «без срока» (все существующие мастера). Срок ничего не отключает автоматически.
Два служебных поля хранят, о чём суперадмину уже сообщали (ежедневная сводка не дублируется).

Revision ID: x4c0d6e2f8a9
Revises: w3b9c5d1e7f8
Create Date: 2026-10-10 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'x4c0d6e2f8a9'
down_revision: Union[str, None] = 'w3b9c5d1e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('masters', sa.Column('trial_ends_on', sa.Date(), nullable=True))
    op.add_column('masters', sa.Column('trial_expiring_notified_for', sa.Date(), nullable=True))
    op.add_column('masters', sa.Column('trial_expired_notified_on', sa.Date(), nullable=True))


def downgrade() -> None:
    # Внимание: откат удаляет сроки пробных периодов и состояние уведомлений.
    op.drop_column('masters', 'trial_expired_notified_on')
    op.drop_column('masters', 'trial_expiring_notified_for')
    op.drop_column('masters', 'trial_ends_on')
