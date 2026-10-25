import logging

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMemberUpdated

from app.services.notifier import MasterNotifier
from app.tenancy import TenantContext, is_owner

logger = logging.getLogger(__name__)

router = Router(name="client_bot_blocked")


@router.my_chat_member(F.chat.type == ChatType.PRIVATE, F.new_chat_member.status == ChatMemberStatus.KICKED)
async def client_blocked_bot(event: ChatMemberUpdated, tenant: TenantContext) -> None:
    """Клиент заблокировал бота (Telegram присылает это как my_chat_member со статусом «kicked»): сообщаем мастеру.

    Одного лишь удаления чата Telegram боту не сообщает — только блокировку, поэтому «удалил и заблокировал»
    приходит именно как блокировка."""
    if is_owner(tenant.master, event.from_user.id):
        return  # мастер сам заблокировал бота — уведомлять некого
    await MasterNotifier(event.bot, master_id=tenant.master_id, owner_telegram_id=tenant.master.owner_telegram_id, session=tenant.session).notify_client_blocked_bot(event.from_user)
