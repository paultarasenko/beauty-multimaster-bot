"""multi-tenant, этап 3: is_in_admin_panel переезжает из users в master_clients

Режим меню владельца (админ-панель / клиентское меню) — свойство связи «человек — мастер», а не человека:
тот, кто владелец мастера A и клиент мастера B, не должен получать один общий флаг на оба бота.
Значения мастера №1 копируются из users в его строки master_clients, затем колонка удаляется из users.

Revision ID: s9d5e1f7a3b4
Revises: r8c4d0e6f2a3
Create Date: 2026-10-07 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 's9d5e1f7a3b4'
down_revision: Union[str, None] = 'r8c4d0e6f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FIRST_MASTER_ID = 1  # флаг в users до этой миграции относится к мастеру №1


def upgrade() -> None:
    op.add_column('master_clients', sa.Column('is_in_admin_panel', sa.Boolean(), server_default='false', nullable=False))

    # Страховка: пользователь с флагом, но без строки master_clients мастера №1, не потеряет режим меню.
    op.execute(
        f"INSERT INTO master_clients (master_id, user_id) "
        f"SELECT {FIRST_MASTER_ID}, u.id FROM users u "
        f"WHERE u.is_in_admin_panel AND NOT EXISTS ("
        f"SELECT 1 FROM master_clients mc WHERE mc.user_id = u.id AND mc.master_id = {FIRST_MASTER_ID})"
    )
    op.execute(
        "UPDATE master_clients mc SET is_in_admin_panel = u.is_in_admin_panel "
        f"FROM users u WHERE u.id = mc.user_id AND mc.master_id = {FIRST_MASTER_ID}"
    )

    op.drop_column('users', 'is_in_admin_panel')


def downgrade() -> None:
    # Внимание: откат возвращает «один мастер» — в users копируется флаг только мастера №1;
    # режимы меню у других мастеров теряются.
    op.add_column('users', sa.Column('is_in_admin_panel', sa.Boolean(), server_default='false', nullable=False))
    op.execute(
        "UPDATE users u SET is_in_admin_panel = mc.is_in_admin_panel "
        f"FROM master_clients mc WHERE mc.user_id = u.id AND mc.master_id = {FIRST_MASTER_ID}"
    )
    op.drop_column('master_clients', 'is_in_admin_panel')
