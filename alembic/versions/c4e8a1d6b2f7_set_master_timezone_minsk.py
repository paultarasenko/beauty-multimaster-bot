"""set master timezone to Europe/Minsk

Revision ID: c4e8a1d6b2f7
Revises: b7d1c2a4f9e3
Create Date: 2026-09-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e8a1d6b2f7'
down_revision: Union[str, None] = 'b7d1c2a4f9e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Бот берёт таймзону из master_profile.timezone (а не из .env), а seed создавал профиль
    # с Europe/Riga. Мастер работает по минскому времени — приводим профиль в соответствие.
    # Уже созданные записи хранятся как момент времени (timestamptz), поэтому не меняются;
    # до 25.10.2026 Рига и Минск в одном часовом поясе (UTC+3).
    op.execute("UPDATE master_profile SET timezone = 'Europe/Minsk'")


def downgrade() -> None:
    op.execute("UPDATE master_profile SET timezone = 'Europe/Riga'")
