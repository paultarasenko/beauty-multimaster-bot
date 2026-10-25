"""Админская часть меню «📸 Портфолио»: добавление и удаление фото, правка названия и описания.

Просмотр и листание (◀️ ▶️) — общий сценарий для клиента и мастера, обрабатывается в
handlers/client/content.py (см. view_portfolio_item и send_portfolio_view), дублировать
здесь не нужно. Здесь — только то, что доступно исключительно администратору.
"""
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.admin.about import _menu_button_texts
from app.bot.handlers.client.content import _portfolio_caption, _portfolio_keyboard, send_portfolio_view
from app.bot.keyboards.admin_keyboards import (
    portfolio_admin_delete_confirm_keyboard,
    portfolio_admin_edit_cancel_keyboard,
    portfolio_admin_empty_keyboard,
)
from app.bot.keyboards.callback_data import AdminPortfolioCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminPortfolioFSM
from app.database.models import PortfolioItem
from app.database.repositories.content_repository import ContentRepository
from app.tenancy import TenantContext

router = Router(name="admin_portfolio")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

SKIP = "-"  # этим символом можно пропустить название/описание
MAX_TITLE_LEN = 255  # столбец portfolio_items.title — String(255)
MAX_DESCRIPTION_LEN = 1000  # описание попадает в подпись к фото вместе с названием
DELETE_SUFFIX = "\n\nУдалить это фото из портфолио?"


def _delete_confirm_caption(item: PortfolioItem) -> str:
    base = _portfolio_caption(item, limit=1024 - len(DELETE_SUFFIX)) or ""
    return f"{base}{DELETE_SUFFIX}" if base else DELETE_SUFFIX.strip()


async def _cancel_if_menu_button(message: Message, state: FSMContext, text: str) -> bool:
    if text.startswith("/") or text in _menu_button_texts():
        await state.clear()
        await message.answer("Добавление фото отменено. Нажмите «📸 Портфолио» ещё раз, если передумаете.")
        return True
    return False


# --- Добавить фото ---


@router.callback_query(AdminPortfolioCallback.filter(F.action == "add"))
async def start_add_portfolio_item(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPortfolioFSM.add_photo)
    await callback.message.answer("Отправьте фото для портфолио.")
    await callback.answer()


@router.message(AdminPortfolioFSM.add_photo, F.photo)
async def receive_portfolio_photo(message: Message, state: FSMContext) -> None:
    # Последний элемент — вариант с наибольшим разрешением.
    file_id = message.photo[-1].file_id
    await state.update_data(file_id=file_id)
    await state.set_state(AdminPortfolioFSM.add_title)
    await message.answer(f"Введите название фото (или отправьте «{SKIP}», чтобы пропустить).")


@router.message(AdminPortfolioFSM.add_photo)
async def receive_portfolio_photo_invalid(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if await _cancel_if_menu_button(message, state, text):
        return
    await message.answer("Отправьте именно фото (не текст и не файл-документ).")


@router.message(AdminPortfolioFSM.add_title)
async def receive_portfolio_title(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if await _cancel_if_menu_button(message, state, text):
        return
    if not text:
        await message.answer(f"Введите название текстом или «{SKIP}», чтобы пропустить.")
        return
    if len(text) > MAX_TITLE_LEN:
        await message.answer(f"Название слишком длинное: максимум {MAX_TITLE_LEN} символов.")
        return
    if "<" in text or ">" in text:
        await message.answer("Символы < и > использовать нельзя. Отправьте текст без них.")
        return

    await state.update_data(title=None if text == SKIP else text)
    await state.set_state(AdminPortfolioFSM.add_description)
    await message.answer(f"Введите описание (или «{SKIP}», чтобы пропустить).")


@router.message(AdminPortfolioFSM.add_description)
async def receive_portfolio_description(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = (message.text or "").strip()
    if await _cancel_if_menu_button(message, state, text):
        return
    if not text:
        await message.answer(f"Введите описание текстом или «{SKIP}», чтобы пропустить.")
        return
    if len(text) > MAX_DESCRIPTION_LEN:
        await message.answer(f"Слишком длинное описание: максимум {MAX_DESCRIPTION_LEN} символов.")
        return
    if "<" in text or ">" in text:
        await message.answer("Символы < и > использовать нельзя. Отправьте текст без них.")
        return

    data = await state.get_data()
    items = await ContentRepository(session, tenant.master_id).get_active_portfolio()
    next_sort_order = max((i.sort_order for i in items), default=-1) + 1

    item = PortfolioItem(
        master_id=tenant.master_id,
        telegram_file_id=data["file_id"],
        title=data.get("title"),
        description=None if text == SKIP else text,
        sort_order=next_sort_order,
        active=True,
    )
    session.add(item)
    await session.commit()
    await state.clear()
    await message.answer("✅ Фото добавлено в портфолио.")
    await send_portfolio_view(message, session, admin_view=True, tenant=tenant)


# --- Удалить фото ---


@router.callback_query(AdminPortfolioCallback.filter(F.action == "delete"))
async def ask_delete_portfolio_item(
    callback: CallbackQuery, callback_data: AdminPortfolioCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    item = await ContentRepository(session, tenant.master_id).get_portfolio_item(callback_data.item_id)
    if item is None:
        await callback.answer("Это фото уже удалено.", show_alert=True)
        return
    await callback.message.edit_caption(
        caption=_delete_confirm_caption(item),
        reply_markup=portfolio_admin_delete_confirm_keyboard(item.id),
    )
    await callback.answer()


@router.callback_query(AdminPortfolioCallback.filter(F.action == "delete_confirm"))
async def confirm_delete_portfolio_item(
    callback: CallbackQuery, callback_data: AdminPortfolioCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    item = await ContentRepository(session, tenant.master_id).get_portfolio_item(callback_data.item_id)
    if item is None:
        await callback.answer("Это фото уже удалено.", show_alert=True)
        return

    await session.delete(item)
    await session.commit()

    items = await ContentRepository(session, tenant.master_id).get_active_portfolio()
    if not items:
        await callback.message.delete()
        await callback.message.answer(
            "✅ Фото удалено. Портфолио теперь пусто.", reply_markup=portfolio_admin_empty_keyboard()
        )
        await callback.answer()
        return

    next_item = items[0]
    keyboard = _portfolio_keyboard([i.id for i in items], next_item.id, admin_view=True)
    media = InputMediaPhoto(media=next_item.telegram_file_id, caption=_portfolio_caption(next_item))
    await callback.message.edit_media(media=media, reply_markup=keyboard)
    await callback.answer("Удалено.")


# --- Изменить название / описание ---


async def _send_portfolio_item(message: Message, session: AsyncSession, item_id: int, tenant: TenantContext) -> None:
    """Присылает карточку конкретного фото (с клавиатурой мастера). Если фото уже нет — открывает портфолио сначала."""
    items = await ContentRepository(session, tenant.master_id).get_active_portfolio()
    item = next((i for i in items if i.id == item_id), None)
    if item is None:
        await send_portfolio_view(message, session, admin_view=True, tenant=tenant)
        return
    keyboard = _portfolio_keyboard([i.id for i in items], item.id, admin_view=True)
    await message.answer_photo(item.telegram_file_id, caption=_portfolio_caption(item), reply_markup=keyboard)


async def _start_edit(
    callback: CallbackQuery, callback_data: AdminPortfolioCallback, session: AsyncSession, state: FSMContext, field: str, tenant: TenantContext
) -> None:
    item = await ContentRepository(session, tenant.master_id).get_portfolio_item(callback_data.item_id)
    if item is None:
        await callback.answer("Это фото уже удалено.", show_alert=True)
        return

    label = "название" if field == "title" else "описание"
    current = getattr(item, field)
    current_text = f"Сейчас: {escape(current)}" if current else "Сейчас не задано."
    await state.set_state(AdminPortfolioFSM.edit_title if field == "title" else AdminPortfolioFSM.edit_description)
    await state.update_data(item_id=item.id)
    await callback.message.answer(
        f"Введите новое {label}.\n{current_text}\n\nОтправьте «{SKIP}», чтобы убрать {label}.",
        reply_markup=portfolio_admin_edit_cancel_keyboard(item.id),
    )
    await callback.answer()


async def _save_edit(message: Message, state: FSMContext, session: AsyncSession, field: str, tenant: TenantContext) -> None:
    label = "название" if field == "title" else "описание"
    max_len = MAX_TITLE_LEN if field == "title" else MAX_DESCRIPTION_LEN

    text = (message.text or "").strip()
    if not text:
        await message.answer(f"Введите {label} текстом или «{SKIP}», чтобы убрать.")
        return
    if len(text) > max_len:
        await message.answer(f"Слишком длинный текст: максимум {max_len} символов.")
        return
    if "<" in text or ">" in text:
        await message.answer("Символы < и > использовать нельзя. Отправьте текст без них.")
        return

    data = await state.get_data()
    item_id = data.get("item_id")
    item = await ContentRepository(session, tenant.master_id).get_portfolio_item(item_id)
    if item is None:
        await state.clear()
        await message.answer("Это фото уже удалено.")
        return

    setattr(item, field, None if text == SKIP else text)
    await session.commit()
    await state.clear()
    await message.answer(f"✅ {label.capitalize()} {'убрано' if text == SKIP else 'обновлено'}.")
    await _send_portfolio_item(message, session, item_id, tenant=tenant)


@router.callback_query(AdminPortfolioCallback.filter(F.action == "edit_title"))
async def start_edit_portfolio_title(
    callback: CallbackQuery, callback_data: AdminPortfolioCallback, session: AsyncSession, state: FSMContext, tenant: TenantContext
) -> None:
    await _start_edit(callback, callback_data, session, state, "title", tenant=tenant)


@router.callback_query(AdminPortfolioCallback.filter(F.action == "edit_description"))
async def start_edit_portfolio_description(
    callback: CallbackQuery, callback_data: AdminPortfolioCallback, session: AsyncSession, state: FSMContext, tenant: TenantContext
) -> None:
    await _start_edit(callback, callback_data, session, state, "description", tenant=tenant)


@router.message(AdminPortfolioFSM.edit_title)
async def receive_edited_portfolio_title(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _save_edit(message, state, session, "title", tenant=tenant)


@router.message(AdminPortfolioFSM.edit_description)
async def receive_edited_portfolio_description(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await _save_edit(message, state, session, "description", tenant=tenant)


@router.callback_query(AdminPortfolioCallback.filter(F.action == "edit_cancel"))
async def cancel_portfolio_edit(callback: CallbackQuery, state: FSMContext) -> None:
    # Сбрасываем только ввод правки: если мастер уже начал другой сценарий, его состояние не трогаем.
    if await state.get_state() in (AdminPortfolioFSM.edit_title.state, AdminPortfolioFSM.edit_description.state):
        await state.clear()
    await callback.message.edit_text("Редактирование отменено.")
    await callback.answer()
