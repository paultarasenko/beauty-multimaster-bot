"""Админская часть меню «⭐ Отзывы»: удаление отзыва и кнопка «Назад».

Кнопки «📖 Посмотреть» и «🖊️ Оставить» в этом же меню используют общие callback'и
ReviewsCallback/ReviewsPageCallback/ReviewRatingCallback — это тот же сценарий, что и у
клиента, обрабатывается в handlers/client/content.py, дублировать здесь не нужно.
Здесь — только то, что доступно исключительно администратору.
"""
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.client.content import get_reviews_summary_text
from app.bot.keyboards.admin_keyboards import (
    admin_review_delete_confirm_keyboard,
    admin_review_delete_list_keyboard,
    admin_reviews_menu_keyboard,
)
from app.bot.keyboards.callback_data import AdminReviewDeleteCallback, AdminReviewsCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.database.models import Review
from app.database.repositories.content_repository import ContentRepository
from app.tenancy import TenantContext

router = Router(name="admin_reviews")
router.callback_query.filter(IsAdminFilter())


def _delete_list_items(reviews: list[Review]) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    for r in reviews:
        # У Telegram лимит на текст inline-кнопки — 64 символа, а author_name может быть
        # до 100 символов (см. модель Review), поэтому обрезаем для подписи кнопки.
        name = (r.author_name or "Без имени").strip()
        if len(name) > 40:
            name = name[:39] + "…"
        items.append((r.id, f"{'⭐' * r.rating} {name}"))
    return items


@router.callback_query(AdminReviewsCallback.filter(F.action == "back"))
async def back_to_reviews_menu(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    text = await get_reviews_summary_text(session, tenant=tenant)
    await callback.message.edit_text(text, reply_markup=admin_reviews_menu_keyboard())
    await callback.answer()


@router.callback_query(AdminReviewDeleteCallback.filter(F.action == "list"))
async def show_delete_list(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    reviews = await ContentRepository(session, tenant.master_id).get_active_reviews()
    if not reviews:
        await callback.answer("Отзывов пока нет.", show_alert=True)
        return
    await callback.message.edit_text(
        "Выберите отзыв для удаления:", reply_markup=admin_review_delete_list_keyboard(_delete_list_items(reviews))
    )
    await callback.answer()


@router.callback_query(AdminReviewDeleteCallback.filter(F.action == "open"))
async def open_review_to_delete(
    callback: CallbackQuery, callback_data: AdminReviewDeleteCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    review = await ContentRepository(session, tenant.master_id).get_review_by_id(callback_data.review_id)
    if review is None:
        await callback.answer("Отзыв не найден.", show_alert=True)
        return
    text = (
        f"👤 {escape(review.author_name or 'Без имени')}\n{'⭐' * review.rating}\n"
        f"{escape(review.text)}\n\nУдалить этот отзыв?"
    )
    await callback.message.edit_text(text, reply_markup=admin_review_delete_confirm_keyboard(review.id))
    await callback.answer()


@router.callback_query(AdminReviewDeleteCallback.filter(F.action == "confirm"))
async def confirm_delete_review(
    callback: CallbackQuery, callback_data: AdminReviewDeleteCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    review = await ContentRepository(session, tenant.master_id).get_review_by_id(callback_data.review_id)
    if review is None:
        await callback.answer("Отзыв не найден.", show_alert=True)
        return

    await session.delete(review)
    await session.commit()

    reviews = await ContentRepository(session, tenant.master_id).get_active_reviews()
    if not reviews:
        await callback.message.edit_text(
            "✅ Отзыв удалён. Отзывов больше нет.", reply_markup=admin_reviews_menu_keyboard()
        )
    else:
        await callback.message.edit_text(
            "✅ Отзыв удалён.\n\nВыберите отзыв для удаления:",
            reply_markup=admin_review_delete_list_keyboard(_delete_list_items(reviews)),
        )
    await callback.answer()