"""Бот мастера для раздела «🤖 Мой бот» (этап 9): читает ТОЛЬКО бот текущего мастера. Токен и секреты наружу не отдаются."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MasterBot


@dataclass(frozen=True)
class MyBotInfo:
    telegram_bot_id: int
    bot_username: str | None
    is_active: bool


async def get_my_bot(session: AsyncSession, master_id: int) -> MyBotInfo | None:
    row = (await session.execute(select(MasterBot).where(MasterBot.master_id == master_id))).scalar_one_or_none()
    if row is None:
        return None
    return MyBotInfo(telegram_bot_id=row.telegram_bot_id, bot_username=row.bot_username, is_active=row.is_active)
