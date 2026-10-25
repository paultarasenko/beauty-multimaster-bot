import logging

from typing import Union

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from app.database.models import Master
from app.tenancy import TenantContext, is_owner

logger = logging.getLogger(__name__)


async def get_admin_contact_username(bot: Bot, master: Master) -> str | None:
    """Username владельца мастера (для кнопки-ссылки на чат с ним), определяется по master.owner_telegram_id,
    а не хранится отдельно в БД — так кнопка всегда ведёт на того, кто сейчас назначен владельцем.
    Если владельца нет, у аккаунта нет username или чат ещё недоступен боту, возвращает None — кнопку тогда не показываем.
    """
    if master.owner_telegram_id is None:
        return None
    try:
        chat = await bot.get_chat(master.owner_telegram_id)
    except Exception:
        logger.exception("Не удалось получить chat владельца (master_id=%s, owner_telegram_id=%s)", master.id, master.owner_telegram_id)
        return None
    return chat.username


class IsAdminFilter(BaseFilter):
    """Пропускает апдейт дальше только если отправитель — владелец мастера этого апдейта
    (from_user.id == tenant.master.owner_telegram_id). Мастер апдейта берётся из data["tenant"]
    (кладёт TenantMiddleware); без него апдейт не пропускается.
    Работает и для Message, и для CallbackQuery — применяется на router.message
    И router.callback_query каждого admin-роутера, иначе callback-хендлеры
    (инлайн-кнопки в админке) остаются доступны кому угодно, кто пришлёт нужный callback_data.
    """

    async def __call__(self, event: Union[Message, CallbackQuery], tenant: TenantContext | None = None) -> bool:
        return tenant is not None and event.from_user is not None and is_owner(tenant.master, event.from_user.id)
