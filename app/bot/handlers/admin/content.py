from html import escape
from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.admin.about import send_editable_card, show_about_me
from app.bot.handlers.client.content import get_reviews_summary_text, send_portfolio_view, send_reviews_entry
from app.bot.keyboards.admin_keyboards import admin_reviews_menu_keyboard
from app.bot.menu_mode import is_admin_mode
from app.bot.middlewares.admin_filter import get_admin_contact_username
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.service_repository import ServiceRepository
from app.utils.duration import format_duration
from app.utils.money import format_money
from app.tenancy import TenantContext

router = Router(name="client_content")


@router.message(F.text == "💰 Услуги и цены")
async def show_price(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    services = await ServiceRepository(session, tenant.master_id).get_active()
    if not services:
        await message.answer("Прайс пока пуст — загляните позже 🙂")
        return
    currency = (await tenant.get_settings()).currency
    for s in services:
        text = f"{escape(s.name)}\n\n💰 {format_money(s.price, currency)}\n⏱ ~ {format_duration(s.duration_minutes)}"
        if s.description:
            text += f"\n\n{escape(s.description)}"
        await message.answer(text)


@router.message(F.text == "📸 Портфолио")
async def show_portfolio(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    # Мастер в админ-панели видит тот же просмотр, но с кнопками «➕ Добавить»/«🗑️ Удалить»
    # (см. portfolio_admin_keyboard) — листание работает как у клиента (общий PortfolioCallback).
    admin_view = await is_admin_mode(session, tenant, message.from_user.id)
    await send_portfolio_view(message, session, admin_view=admin_view, tenant=tenant)


@router.message(F.text == "✨ Обо мне")
async def show_about(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    # Та же кнопка есть в админском меню «Информация»: мастер в админ-панели открывает редактор, а не клиентский просмотр.
    if await is_admin_mode(session, tenant, message.from_user.id):
        await show_about_me(message)
        return
    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    if profile is None or not profile.description:
        await message.answer("Информация о мастере пока не заполнена.")
        return
    # Клиенту показываем только текст, который написал мастер (без имени, эмодзи и прочего).
    text = escape(profile.description)
    if profile.photo_file_id and len(profile.description) <= 1024:  # лимит Telegram на подпись к фото
        await message.answer_photo(profile.photo_file_id, caption=text)
    elif profile.photo_file_id:
        await message.answer_photo(profile.photo_file_id)
        await message.answer(text)
    else:
        await message.answer(text)


@router.message(F.text == "📍 Как меня найти")
async def show_location(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    # Мастер в админ-панели видит тот же текст, но с кнопкой «Редактировать».
    if profile is not None and await is_admin_mode(session, tenant, message.from_user.id):
        await send_editable_card(message, profile, "address")
        return
    if profile is None or not profile.address:
        await message.answer("Адрес пока не указан.")
        return
    # Клиенту показываем только текст, который написал мастер (без эмодзи и прочего).
    await message.answer(escape(profile.address))


@router.message(F.text == "⭐ Отзывы")
async def show_reviews(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    # Мастер в админ-панели видит ту же сводку, но с дополнительной кнопкой «Удалить»
    # (см. admin_reviews_menu_keyboard) — «Посмотреть»/«Оставить» работают как у клиента.
    if await is_admin_mode(session, tenant, message.from_user.id):
        text = await get_reviews_summary_text(session, tenant=tenant)
        await message.answer(text, reply_markup=admin_reviews_menu_keyboard())
        return
    await send_reviews_entry(message, session, tenant=tenant)


@router.message(F.text == "📞 Связаться со мной")
async def show_contact(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    contact_username = await get_admin_contact_username(message.bot, tenant.master)
    if not contact_username:
        await message.answer("Контакт мастера пока не указан.")
        return
    await message.answer(f"📞 Написать мастеру: @{contact_username}")