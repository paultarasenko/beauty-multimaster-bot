from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.callback_data import AdminAccessRequestCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.services.access_request_service import BLOCKED_TEXT, OPENED_TEXT, decide_access_request, notify_client
from app.tenancy import TenantContext

router = Router(name="admin_access_requests")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

OPENED_NOTE = "✅ Доступ открыт"
BLOCKED_NOTE = "🔒 Ограничено навсегда"


@router.callback_query(AdminAccessRequestCallback.filter())
async def decide(
    callback: CallbackQuery, callback_data: AdminAccessRequestCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    open_access = callback_data.action == "open"
    decided = await decide_access_request(session, tenant.master_id, callback_data.request_id, open_access)
    if decided is None:
        # Повторное нажатие или обращение уже обработано — ничего не ломаем.
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.answer("Это обращение уже обработано.", show_alert=True)
        return
    _, client = decided
    await session.commit()

    note = OPENED_NOTE if open_access else BLOCKED_NOTE
    await callback.message.edit_text(f"{callback.message.html_text}\n\n{note}", reply_markup=None)
    delivered = await notify_client(callback.bot, client, OPENED_TEXT if open_access else BLOCKED_TEXT)
    await callback.answer(None if delivered else "Решение сохранено, но клиенту доставить сообщение не удалось.", show_alert=not delivered)
