from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.callback_data import MasterRequestCallback
from app.bot.keyboards.client_keyboards import master_request_cancel_keyboard
from app.bot.states.booking_states import MasterRequestFSM
from app.database.repositories.user_repository import UserRepository
from app.services.booking_service import booking_service_for
from app.services.access_request_service import BLOCKED_TEXT
from app.services.exceptions import BookingLimitReachedError, ClientBlockedError, ClientRestrictedError
from app.services.limit_request_service import MAX_REQUEST_LEN, create_request, get_pending_request
from app.services.notifier import MasterNotifier
from app.utils.timezone import get_master_tz
from app.tenancy import TenantContext

router = Router(name="client_master_request")

PENDING_TEXT = "Ваш запрос уже отправлен мастеру. Пожалуйста, дождитесь его решения."


@router.callback_query(MasterRequestCallback.filter(F.action == "write"))
async def ask_message(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer()
        return
    if await get_pending_request(session, tenant.master_id, user.id) is not None:
        await callback.answer(PENDING_TEXT, show_alert=True)
        return

    tz = await get_master_tz(session, tenant.master_id)
    try:
        await (await booking_service_for(session, tz, tenant)).ensure_client_can_book(user.id)
    except ClientBlockedError:
        await callback.message.answer(BLOCKED_TEXT)
        await callback.answer()
        return
    except ClientRestrictedError:
        await callback.answer("К сожалению, запись для вас сейчас недоступна. Свяжитесь с мастером.", show_alert=True)
        return
    except BookingLimitReachedError:
        pass
    else:
        # Место уже освободилось (или мастер уже дал разрешение) — писать мастеру незачем.
        await callback.answer("Вы уже можете записаться: нажмите «📅 Записаться».", show_alert=True)
        return

    await state.set_state(MasterRequestFSM.waiting_message)
    await callback.message.answer(
        "✉️ Напишите сообщение для мастера.",
        reply_markup=master_request_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(MasterRequestCallback.filter(F.action == "cancel"))
async def cancel_message(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.message(MasterRequestFSM.waiting_message, F.text)
async def receive_message(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = message.text.strip()
    if not text:
        await message.answer("Сообщение не должно быть пустым. Напишите текст и отправьте ещё раз.")
        return
    if len(text) > MAX_REQUEST_LEN:
        await message.answer(
            f"Сообщение слишком длинное: {len(text)} символов, максимум {MAX_REQUEST_LEN}. "
            "Сократите его и отправьте ещё раз."
        )
        return

    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(message.from_user.id)
    if user is None:
        await state.clear()
        return

    request = await create_request(session, tenant.master_id, user.id, text)
    if request is None:
        await state.clear()
        await message.answer(PENDING_TEXT)
        return
    await session.commit()

    if not await MasterNotifier(message.bot, master_id=tenant.master_id, owner_telegram_id=tenant.master.owner_telegram_id, session=tenant.session).notify_limit_request(request, user):
        # Мастер не получил запрос — не оставляем его «висеть», иначе клиент не сможет написать ещё раз.
        await session.delete(request)
        await session.commit()
        await state.clear()
        await message.answer("Не удалось передать сообщение мастеру. Попробуйте позже или свяжитесь с ним напрямую.")
        return

    await state.clear()
    await message.answer("✅ Сообщение передано мастеру. Ответ придёт в этот чат.")


@router.message(MasterRequestFSM.waiting_message)
async def receive_non_text(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте сообщение текстом.")
