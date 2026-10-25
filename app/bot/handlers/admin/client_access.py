import datetime as dt

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import client_access_keyboard
from app.bot.keyboards.callback_data import AdminClientAccessCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.database.repositories.user_repository import UserRepository
from app.services.access_request_service import OPENED_TEXT, notify_client
from app.services.booking_service import booking_service_for
from app.services.limit_request_service import burn_unused_approvals
from app.services.notifier import MasterNotifier
from app.utils.timezone import get_master_tz
from app.tenancy import TenantContext

router = Router(name="admin_client_access")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

RESTRICTED_NOTE = "🚫 Доступ ограничен: клиент больше не может записываться."
ALLOWED_NOTE = "✅ Доступ подтверждён."


async def _finish(callback: CallbackQuery, note: str, keyboard) -> None:
    """Дописывает решение мастера под текстом предупреждения (html_text сохраняет экранирование имени)."""
    base = (callback.message.html_text or "").split("\n\n" + RESTRICTED_NOTE)[0].split("\n\n" + ALLOWED_NOTE)[0]
    await callback.message.edit_text(f"{base}\n\n{note}", reply_markup=keyboard)


@router.callback_query(AdminClientAccessCallback.filter(F.action == "restrict"))
async def restrict_client(
    callback: CallbackQuery, callback_data: AdminClientAccessCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    users = UserRepository(session, tenant.master_id)
    user = await users.get_by_id(callback_data.user_id)
    client = await users.get_client(callback_data.user_id)
    if user is None or client is None:
        await callback.answer("Клиент не найден.", show_alert=True)
        return
    # Все будущие записи клиента отменяются (слоты освобождаются), новые записи закрыты.
    was_restricted = client.is_restricted
    tz = await get_master_tz(session, tenant.master_id)
    cancelled = await (await booking_service_for(session, tz, tenant)).cancel_all_upcoming(user.id)
    client.is_restricted = True
    client.cancel_streak_reset_at = dt.datetime.now(dt.timezone.utc)  # серия отмен обнуляется, как и при «Подтвердить»
    client.over_limit_cancels = None
    await burn_unused_approvals(session, tenant.master_id, user.id)  # разовое разрешение на запись сверх лимита сгорает
    await session.commit()
    await _finish(callback, RESTRICTED_NOTE, client_access_keyboard(user.id, restricted=True))
    await callback.answer("Доступ ограничен.")

    # Повторное нажатие (клиент уже ограничен, отменять нечего) не должно слать лишние сообщения.
    if cancelled or not was_restricted:
        notifier = MasterNotifier(callback.bot, master_id=tenant.master_id, owner_telegram_id=tenant.master.owner_telegram_id, session=tenant.session)
        await notifier.notify_client_restricted(user, cancelled, tz, (await tenant.get_settings()).currency)
        await notifier.notify_master_client_restricted(user, cancelled, tz)


@router.callback_query(AdminClientAccessCallback.filter(F.action == "allow"))
async def allow_client(
    callback: CallbackQuery, callback_data: AdminClientAccessCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    users = UserRepository(session, tenant.master_id)
    user = await users.get_by_id(callback_data.user_id)
    client = await users.get_client(callback_data.user_id)
    if user is None or client is None:
        await callback.answer("Клиент не найден.", show_alert=True)
        return
    # Клиенту пишем только если доступ реально возвращается: «Подтвердить» есть и под предупреждением,
    # когда клиент ещё не ограничен, — тогда для него ничего не меняется и «открыл доступ» было бы неправдой.
    was_restricted = client.is_restricted or client.is_blocked_forever
    # Снимаем ограничение (если оно было) и обнуляем серию: следующие три отмены подряд снова вызовут предупреждение.
    client.is_restricted = False
    client.is_blocked_forever = False
    client.cancel_streak_reset_at = dt.datetime.now(dt.timezone.utc)
    client.over_limit_cancels = None
    await session.commit()
    await _finish(callback, ALLOWED_NOTE, None)

    delivered = await notify_client(callback.bot, user, OPENED_TEXT) if was_restricted else True
    if delivered:
        await callback.answer("Доступ подтверждён.")
    else:
        await callback.answer("Доступ подтверждён, но клиенту доставить сообщение не удалось.", show_alert=True)
