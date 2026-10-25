"""multi-tenant, этап 8: индивидуальные настройки мастера (master_settings) + профили для мастеров без них

Одна строка на мастера (UNIQUE master_id, FK → masters ON DELETE CASCADE), типизированные колонки с CHECK-ограничениями.
Значения по умолчанию (server_default) равны поведению бота до этапа 8, поэтому для всех существующих мастеров
строка создаётся только с master_id — остальное заполняют значения по умолчанию.

Заодно у мастеров, у которых нет master_profile, создаётся профиль (чтобы не было «профиль не найден»): часовой пояс —
Europe/Minsk (прежнее значение запасного settings.timezone по умолчанию; изменить его можно в профиле).

Revision ID: v2a8b4c0d6e7
Revises: u1f7a3b9c5d6
Create Date: 2026-10-08 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'v2a8b4c0d6e7'
down_revision: Union[str, None] = 'u1f7a3b9c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'master_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('master_id', sa.Integer(), nullable=False),
        sa.Column('slot_interval_minutes', sa.Integer(), server_default='30', nullable=False),
        sa.Column('reminder_24h_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('reminder_2h_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('master_reminder_2h_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('max_active_bookings', sa.Integer(), server_default='3', nullable=False),
        sa.Column('cancel_streak_limit', sa.Integer(), server_default='3', nullable=False),
        sa.Column('allow_reschedule', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('allow_reviews', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('currency', sa.String(length=8), server_default='BYN', nullable=False),
        sa.Column('booking_confirmation_text', sa.Text(), nullable=True),
        sa.Column('cancellation_text', sa.Text(), nullable=True),
        sa.Column('reminder_24h_text', sa.Text(), nullable=True),
        sa.Column('reminder_2h_text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['master_id'], ['masters.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('master_id', name='uq_master_settings_master_id'),
        sa.CheckConstraint('slot_interval_minutes IN (15, 30, 60)', name='ck_master_settings_slot_interval'),
        sa.CheckConstraint('max_active_bookings BETWEEN 1 AND 10', name='ck_master_settings_max_active_bookings'),
        sa.CheckConstraint('cancel_streak_limit BETWEEN 2 AND 10', name='ck_master_settings_cancel_streak_limit'),
        sa.CheckConstraint('char_length(currency) BETWEEN 1 AND 8', name='ck_master_settings_currency_len'),
        sa.CheckConstraint(
            'booking_confirmation_text IS NULL OR char_length(booking_confirmation_text) <= 1000',
            name='ck_master_settings_confirmation_len',
        ),
        sa.CheckConstraint(
            'cancellation_text IS NULL OR char_length(cancellation_text) <= 1000',
            name='ck_master_settings_cancellation_len',
        ),
        sa.CheckConstraint(
            'reminder_24h_text IS NULL OR char_length(reminder_24h_text) <= 1000',
            name='ck_master_settings_reminder_24h_len',
        ),
        sa.CheckConstraint(
            'reminder_2h_text IS NULL OR char_length(reminder_2h_text) <= 1000',
            name='ck_master_settings_reminder_2h_len',
        ),
    )
    # Настройки по умолчанию — всем существующим мастерам (прежнее поведение бота не меняется).
    op.execute("INSERT INTO master_settings (master_id) SELECT id FROM masters")
    # Профиль — тем, у кого его нет: хендлеры больше не отвечают «профиль не найден».
    op.execute(
        "INSERT INTO master_profile (master_id, timezone) "
        "SELECT m.id, 'Europe/Minsk' FROM masters m "
        "WHERE NOT EXISTS (SELECT 1 FROM master_profile p WHERE p.master_id = m.id)"
    )


def downgrade() -> None:
    # Созданные профили остаются: это обычные данные мастера, они совместимы с прежним кодом.
    # Внимание: откат удаляет индивидуальные настройки мастеров (интервал, валюту, лимиты, тексты).
    op.drop_table('master_settings')
