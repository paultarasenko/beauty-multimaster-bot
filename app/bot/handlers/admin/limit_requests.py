from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import limit_comment_keyboard
from app.bot.keyboards.callback_data import AdminLimitCommentCallback, AdminLimitRequestCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import MasterReplyFSM
from app.services.limit_request_service import MAX_REQUEST_LEN, decide_request, deliver_decision, get_request
from app.tenancy import TenantContext

router = Router(name="admin_limit_requests")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

APPROVED_NOTE = "✅ Принято"
REJECTED_NOTE = "❌ Отклонено"
REQUEST_NOT_FOUND_TEXT = "Запрос не найден."
SENT_TEXT = "✅ Решение отправлено клиенту."
FAILED_TEXT = "⚠️ Не удалось отправить решение клиенту (возможно, он заблокировал бота)."


async def _remove_buttons(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(AdminLimitRequestCallback.filter())
async def decide(
    callback: CallbackQuery, callback_data: AdminLimitRequestCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    approve = callback_data.action == "approve"
    request = await decide_request(session, tenant.master_id, callback_data.request_id, approve)
    if request is None:
        # Повторное нажатие или запрос уже обработан — ничего не ломаем.
        await _remove_buttons(callback)
        await callback.answer("Этот запрос уже обработан.", show_alert=True)
        return
    await session.commit()

    note = APPROVED_NOTE if approve else REJECTED_NOTE
    await callback.message.edit_text(f"{callback.message.html_text}\n\n{note}", reply_markup=None)
    await callback.message.answer("Желаете добавить сообщение?", reply_markup=limit_comment_keyboard(request.id))
    await callback.answer()


@router.callback_query(AdminLimitCommentCallback.filter(F.action == "no"))
async def no_comment(
    callback: CallbackQuery, callback_data: AdminLimitCommentCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    if await get_request(session, tenant.master_id, callback_data.request_id) is None:
        # Нет такого запроса у ЭТОГО мастера (чужой id из подделанной кнопки и несуществующий неразличимы): сообщение
        # мастера не трогаем, клиенту ничего не уходит.
        await callback.answer(REQUEST_NOT_FOUND_TEXT, show_alert=True)
        return
    delivered = await deliver_decision(callback.bot, session, tenant.master_id, callback_data.request_id, None)
    await callback.message.edit_text(SENT_TEXT if delivered else FAILED_TEXT)
    await callback.answer()


@router.callback_query(AdminLimitCommentCallback.filter(F.action == "yes"))
async def ask_comment(
    callback: CallbackQuery,
    callback_data: AdminLimitCommentCallback,
    state: FSMContext,
    session: AsyncSession,
    tenant: TenantContext,
) -> None:
    request = await get_request(session, tenant.master_id, callback_data.request_id)
    if request is None or request.client_notified_at is not None:
        await _remove_buttons(callback)
        await callback.answer("Решение уже отправлено клиенту.", show_alert=True)
        return
    await state.set_state(MasterReplyFSM.waiting_text)
    await state.update_data(request_id=request.id)
    await callback.message.edit_text(f"✍️ Введите сообщение для клиента (до {MAX_REQUEST_LEN} символов).")
    await callback.answer()


@router.message(MasterReplyFSM.waiting_text, F.text)
async def receive_comment(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
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
    data = await state.get_data()
    await state.clear()
    request_id = data.get("request_id")
    # request_id из FSM перепроверяется по мастеру: запрос мог исчезнуть, а состояние — прийти неполным.
    if request_id is None or await get_request(session, tenant.master_id, request_id) is None:
        await message.answer(REQUEST_NOT_FOUND_TEXT)
        return
    delivered = await deliver_decision(message.bot, session, tenant.master_id, request_id, text)
    await message.answer(SENT_TEXT if delivered else FAILED_TEXT)


@router.message(MasterReplyFSM.waiting_text)
async def receive_non_text(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте сообщение текстом.")
