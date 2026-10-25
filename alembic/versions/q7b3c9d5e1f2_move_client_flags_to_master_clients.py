"""multi-tenant, этап 2: клиентские флаги переезжают из users в master_clients

Ограничение доступа, серия отмен и состояние «сверх лимита» — это отношения клиента с конкретным мастером,
а не свойства человека: один Telegram-аккаунт может быть клиентом нескольких мастеров с разными ограничениями.
Данные мастера №1 копируются из users в его строки master_clients (этап 1 создал их для всех пользователей),
затем колонки удаляются из users.

Revision ID: q7b3c9d5e1f2
Revises: p6a2b8c4d0e1
Create Date: 2026-10-07 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'q7b3c9d5e1f2'
down_revision: Union[str, None] = 'p6a2b8c4d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FIRST_MASTER_ID = 1  # все флаги из users до этой миграции принадлежат мастеру №1


def upgrade() -> None:
    op.add_column('master_clients', sa.Column('is_restricted', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('master_clients', sa.Column('is_blocked_forever', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('master_clients', sa.Column('cancel_streak_reset_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('master_clients', sa.Column('over_limit_cancels', sa.Integer(), nullable=True))

    # Страховка: пользователь без строки master_clients (не должно быть после этапа 1) не потеряет флаги.
    op.execute(
        f"INSERT INTO master_clients (master_id, user_id) "
        f"SELECT {FIRST_MASTER_ID}, u.id FROM users u "
        f"WHERE NOT EXISTS (SELECT 1 FROM master_clients mc WHERE mc.user_id = u.id AND mc.master_id = {FIRST_MASTER_ID})"
    )
    op.execute(
        "UPDATE master_clients mc SET "
        "is_restricted = u.is_restricted, "
        "is_blocked_forever = u.is_blocked_forever, "
        "cancel_streak_reset_at = u.cancel_streak_reset_at, "
        "over_limit_cancels = u.over_limit_cancels "
        f"FROM users u WHERE u.id = mc.user_id AND mc.master_id = {FIRST_MASTER_ID}"
    )

    op.drop_column('users', 'over_limit_cancels')
    op.drop_column('users', 'cancel_streak_reset_at')
    op.drop_column('users', 'is_blocked_forever')
    op.drop_column('users', 'is_restricted')


def downgrade() -> None:
    # Внимание: откат возвращает «один мастер» — в users копируются флаги только мастера №1;
    # ограничения клиентов других мастеров теряются.
    op.add_column('users', sa.Column('is_restricted', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('users', sa.Column('is_blocked_forever', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('users', sa.Column('cancel_streak_reset_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('over_limit_cancels', sa.Integer(), nullable=True))

    op.execute(
        "UPDATE users u SET "
        "is_restricted = mc.is_restricted, "
        "is_blocked_forever = mc.is_blocked_forever, "
        "cancel_streak_reset_at = mc.cancel_streak_reset_at, "
        "over_limit_cancels = mc.over_limit_cancels "
        f"FROM master_clients mc WHERE mc.user_id = u.id AND mc.master_id = {FIRST_MASTER_ID}"
    )

    op.drop_column('master_clients', 'over_limit_cancels')
    op.drop_column('master_clients', 'cancel_streak_reset_at')
    op.drop_column('master_clients', 'is_blocked_forever')
    op.drop_column('master_clients', 'is_restricted')
