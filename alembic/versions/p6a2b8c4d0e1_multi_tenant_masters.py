"""multi-tenant, этап 1: masters, master_clients, master_id во всех таблицах данных мастера

Существующие данные становятся данными мастера №1 (id=1; владелец — ADMIN_TELEGRAM_ID из настроек).
master_id получает ПЕРЕХОДНЫЙ server_default='1': код приложения пока не передаёт master_id явно
(это этап 2), и без default вставки упали бы. На этапе 2 default убирается.

Revision ID: p6a2b8c4d0e1
Revises: o5e1f7a3b9c0
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'p6a2b8c4d0e1'
down_revision: Union[str, None] = 'o5e1f7a3b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (таблица, нужен ли отдельный индекс по master_id). Без индекса — там, где master_id и так
# первая колонка уникального ограничения (services, working_hours, master_profile).
TENANT_TABLES: list[tuple[str, bool]] = [
    ('services', False),
    ('working_hours', False),
    ('breaks', True),
    ('blocked_periods', True),
    ('bookings', True),
    ('reviews', True),
    ('portfolio_items', True),
    ('notifications', True),
    ('admin_reminders', True),
    ('client_access_requests', True),
    ('booking_limit_requests', True),
    ('master_profile', False),
]


def upgrade() -> None:
    # --- masters + мастер №1 ---
    op.create_table(
        'masters',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_telegram_id', sa.BigInteger(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_masters_owner_telegram_id'), 'masters', ['owner_telegram_id'], unique=False)

    from app.config.settings import settings

    masters = sa.table(
        'masters',
        sa.column('id', sa.Integer()),
        sa.column('owner_telegram_id', sa.BigInteger()),
        sa.column('is_active', sa.Boolean()),
    )
    op.bulk_insert(masters, [{'id': 1, 'owner_telegram_id': settings.admin_telegram_id or None, 'is_active': True}])
    # Явный id=1 не двигает последовательность — следующий мастер должен получить id=2, а не упасть на дубликате.
    op.execute("SELECT setval(pg_get_serial_sequence('masters', 'id'), 1, true)")

    # --- master_id во всех таблицах данных мастера (существующие строки получают 1) ---
    for table, with_index in TENANT_TABLES:
        op.add_column(table, sa.Column('master_id', sa.Integer(), server_default='1', nullable=False))
        op.create_foreign_key(f'fk_{table}_master_id_masters', table, 'masters', ['master_id'], ['id'], ondelete='CASCADE')
        if with_index:
            op.create_index(op.f(f'ix_{table}_master_id'), table, ['master_id'], unique=False)

    # --- master_profile: один профиль на мастера ---
    op.create_unique_constraint('uq_master_profile_master_id', 'master_profile', ['master_id'])

    # --- master_clients: клиент мастера (users остаётся глобальной личностью по telegram_id) ---
    op.create_table(
        'master_clients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), server_default='1', nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['master_id'], ['masters.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('master_id', 'user_id', name='uq_master_clients_master_user'),
    )
    op.create_index(op.f('ix_master_clients_user_id'), 'master_clients', ['user_id'], unique=False)
    op.execute("INSERT INTO master_clients (master_id, user_id) SELECT 1, id FROM users")

    # --- расписание: уникальность дня недели — в пределах мастера ---
    op.drop_constraint('uq_working_hours_weekday', 'working_hours', type_='unique')
    op.create_unique_constraint('uq_working_hours_master_weekday', 'working_hours', ['master_id', 'weekday'])

    # --- запись принадлежит тому же мастеру, что и её услуга; уведомление — тому же, что и запись ---
    op.create_unique_constraint('uq_services_master_id_id', 'services', ['master_id', 'id'])
    op.create_unique_constraint('uq_bookings_master_id_id', 'bookings', ['master_id', 'id'])
    op.create_foreign_key(
        'fk_bookings_master_service', 'bookings', 'services', ['master_id', 'service_id'], ['master_id', 'id']
    )
    op.create_foreign_key(
        'fk_notifications_master_booking', 'notifications', 'bookings', ['master_id', 'booking_id'], ['master_id', 'id']
    )

    # --- защита от double booking — в пределах мастера (btree_gist уже включён начальной миграцией) ---
    op.execute('ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap_confirmed')
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap_confirmed "
        "EXCLUDE USING gist (master_id WITH =, time_range WITH &&) WHERE (status = 'confirmed')"
    )

    # --- «не больше одного ожидающего обращения» — у клиента к одному мастеру ---
    for table, index_name in (
        ('client_access_requests', 'uq_client_access_requests_one_pending'),
        ('booking_limit_requests', 'uq_booking_limit_requests_one_pending'),
    ):
        op.drop_index(index_name, table_name=table)
        op.create_index(
            index_name, table, ['master_id', 'user_id'], unique=True, postgresql_where=sa.text("status = 'pending'")
        )


def downgrade() -> None:
    # Внимание: откат возвращает «один мастер». Если в БД уже есть данные нескольких мастеров, возврат
    # глобальных ограничений (уникальность weekday, одно ожидающее обращение на клиента) может упасть на дубликатах.
    for table, index_name in (
        ('client_access_requests', 'uq_client_access_requests_one_pending'),
        ('booking_limit_requests', 'uq_booking_limit_requests_one_pending'),
    ):
        op.drop_index(index_name, table_name=table)
        op.create_index(index_name, table, ['user_id'], unique=True, postgresql_where=sa.text("status = 'pending'"))

    op.execute('ALTER TABLE bookings DROP CONSTRAINT ex_bookings_no_overlap_confirmed')
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap_confirmed "
        "EXCLUDE USING gist (time_range WITH &&) WHERE (status = 'confirmed')"
    )

    op.drop_constraint('fk_notifications_master_booking', 'notifications', type_='foreignkey')
    op.drop_constraint('fk_bookings_master_service', 'bookings', type_='foreignkey')
    op.drop_constraint('uq_bookings_master_id_id', 'bookings', type_='unique')
    op.drop_constraint('uq_services_master_id_id', 'services', type_='unique')

    op.drop_constraint('uq_working_hours_master_weekday', 'working_hours', type_='unique')
    op.create_unique_constraint('uq_working_hours_weekday', 'working_hours', ['weekday'])

    op.drop_index(op.f('ix_master_clients_user_id'), table_name='master_clients')
    op.drop_table('master_clients')

    op.drop_constraint('uq_master_profile_master_id', 'master_profile', type_='unique')

    for table, with_index in reversed(TENANT_TABLES):
        if with_index:
            op.drop_index(op.f(f'ix_{table}_master_id'), table_name=table)
        op.drop_constraint(f'fk_{table}_master_id_masters', table, type_='foreignkey')
        op.drop_column(table, 'master_id')

    op.drop_index(op.f('ix_masters_owner_telegram_id'), table_name='masters')
    op.drop_table('masters')
