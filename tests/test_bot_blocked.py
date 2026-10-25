"""Клиент заблокировал бота — мастеру приходит уведомление."""

import datetime as dt

from aiogram.enums import ChatMemberStatus
from aiogram.types import Chat, ChatMemberBanned, ChatMemberMember, ChatMemberUpdated, Update, User

from app.bot.handlers.client import bot_blocked
from tests.conftest import set_owner
from tests.tg import BOT_ID, tg_harness

ADMIN_ID = 999
CLIENT_ID = 555


def _update(user_id: int, status: ChatMemberStatus, chat_type: str = "private") -> Update:
    user = User(id=user_id, is_bot=False, first_name="Анна <b>", username="anna_k")
    bot = User(id=BOT_ID, is_bot=True, first_name="bot")
    member = (
        ChatMemberBanned(status=status, user=bot, until_date=0)
        if status == ChatMemberStatus.KICKED
        else ChatMemberMember(status=status, user=bot)
    )
    old = ChatMemberMember(status=ChatMemberStatus.MEMBER, user=bot)
    return Update(
        update_id=1,
        my_chat_member=ChatMemberUpdated(
            chat=Chat(id=user_id, type=chat_type),
            from_user=user,
            date=dt.datetime.now(dt.timezone.utc),
            old_chat_member=old,
            new_chat_member=member,
        ),
    )


async def test_master_is_notified_when_client_blocks_bot(session):
    await set_owner(session, ADMIN_ID)
    async with tg_harness(bot_blocked.router) as h:
        await h.dp.feed_update(h.bot, _update(CLIENT_ID, ChatMemberStatus.KICKED))
        [text] = h.tg.sent_to(ADMIN_ID)
    assert "заблокировал бота" in text and "@anna_k" in text and str(CLIENT_ID) in text
    assert "<b>" not in text  # имя экранировано


async def test_no_notification_when_client_unblocks_bot(session):
    await set_owner(session, ADMIN_ID)
    async with tg_harness(bot_blocked.router) as h:
        await h.dp.feed_update(h.bot, _update(CLIENT_ID, ChatMemberStatus.MEMBER))
        assert h.tg.sent_to(ADMIN_ID) == []


async def test_no_notification_when_master_blocks_own_bot(session):
    await set_owner(session, ADMIN_ID)
    async with tg_harness(bot_blocked.router) as h:
        await h.dp.feed_update(h.bot, _update(ADMIN_ID, ChatMemberStatus.KICKED))
        assert h.tg.requests == []


async def test_group_events_are_ignored(session):
    await set_owner(session, ADMIN_ID)
    async with tg_harness(bot_blocked.router) as h:
        await h.dp.feed_update(h.bot, _update(CLIENT_ID, ChatMemberStatus.KICKED, chat_type="group"))
        assert h.tg.requests == []
