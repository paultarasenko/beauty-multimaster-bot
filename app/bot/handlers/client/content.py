import datetime as dt
import math
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.admin.about import _menu_button_texts
from app.bot.keyboards.admin_keyboards import (
    admin_info_menu_keyboard,
    admin_menu_keyboard,
    admin_reviews_menu_keyboard,
    portfolio_admin_empty_keyboard,
    portfolio_admin_keyboard,
)
from app.bot.keyboards.callback_data import (
    NavCallback,
    PortfolioCallback,
    ReviewRatingCallback,
    ReviewsCallback,
    ReviewsPageCallback,
)
from app.bot.keyboards.client_keyboards import (
    already_reviewed_keyboard,
    info_menu_keyboard,
    main_menu_keyboard,
    portfolio_nav_keyboard,
    review_rating_keyboard,
    reviews_entry_keyboard,
    reviews_list_keyboard,
)
from app.bot.menu_mode import is_admin_mode
from app.bot.middlewares.admin_filter import get_admin_contact_username
from app.bot.states.booking_states import ReviewFSM
from app.services.access_request_service import BLOCKED_TEXT
from app.database.models import PortfolioItem, Review
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.content_repository import ContentRepository
from app.database.repositories.service_repository import ServiceRepository
from app.utils.duration import format_duration
from app.utils.money import format_money
from app.database.repositories.user_repository import UserRepository
from app.utils.timezone import get_master_tz
from app.tenancy import TenantContext, is_owner

router = Router(name="client_content")

REVIEWS_PAGE_SIZE = 5
MAX_AUTHOR_NAME_LEN = 100  # столбец reviews.author_name — String(100)
REVIEWS_RESTRICTED_TEXT = (
    "К сожалению, оставлять и редактировать отзывы сейчас недоступно.\n\n"
    "Вы можете читать отзывы других клиентов."
)
REVIEWS_DISABLED_TEXT = (
    "Мастер временно отключил возможность оставлять отзывы.\n\n"
    "Вы можете читать отзывы других клиентов."
)
MAX_REVIEW_TEXT_LEN = 500  # чтобы 5 отзывов гарантированно помещались в одно сообщение (лимит Telegram — 4096)


async def _reviews_denied_text(session: AsyncSession, tenant: TenantContext, user) -> str | None:
    """Ограниченный и заблокированный клиенты отзывы только читают. None — писать можно.

    Ограничения — у этого мастера (master_clients): у другого мастера тот же человек может быть не ограничен.
    Если мастер выключил отзывы (настройка allow_reviews) — писать и редактировать нельзя никому из клиентов."""
    if not (await tenant.get_settings()).allow_reviews:
        return REVIEWS_DISABLED_TEXT
    if user is None:
        return None
    client = await UserRepository(session, tenant.master_id).get_client(user.id)
    if client is None:
        return None
    if client.is_blocked_forever:
        return BLOCKED_TEXT
    if client.is_restricted:
        return REVIEWS_RESTRICTED_TEXT
    return None


async def _can_write_reviews(session: AsyncSession, telegram_id: int, tenant: TenantContext) -> bool:
    if not (await tenant.get_settings()).allow_reviews and await is_admin_mode(session, tenant, telegram_id):
        return True  # отзыв от имени мастера из админ-панели настройка клиентских отзывов не закрывает
    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(telegram_id)
    return await _reviews_denied_text(session, tenant, user) is None


@router.message(F.text == "ℹ️ Информация")
async def show_info_menu(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    # Из админ-панели открываем админское подменю, из клиентского режима — клиентское.
    if await is_admin_mode(session, tenant, message.from_user.id):
        await message.answer("ℹ️ Выберите раздел", reply_markup=admin_info_menu_keyboard())
        return
    await message.answer(
        "ℹ️ Выберите действие", reply_markup=info_menu_keyboard(is_admin=is_owner(tenant.master, message.from_user.id))
    )


@router.message(F.text == "⬅️ Админ-панель")
async def back_to_main_menu(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    if await is_admin_mode(session, tenant, message.from_user.id):
        await message.answer("✨ Админ-панель мастера", reply_markup=admin_menu_keyboard())
        return

    tz = await get_master_tz(session, tenant.master_id)
    user_repo = UserRepository(session, tenant.master_id)
    user = await user_repo.get_by_telegram_id(message.from_user.id)
    has_active_booking = False
    if user is not None:
        upcoming = await BookingRepository(session, tenant.master_id).get_user_upcoming(user.id, dt.datetime.now(tz))
        has_active_booking = len(upcoming) > 0

    await message.answer(
        "Выберите действие",
        reply_markup=main_menu_keyboard(
            has_active_booking=has_active_booking, is_admin=is_owner(tenant.master, message.from_user.id)
        ),
    )


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


PORTFOLIO_CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото


def _portfolio_caption(item: PortfolioItem, limit: int = PORTFOLIO_CAPTION_LIMIT) -> str | None:
    parts = [escape(part) for part in (item.title, item.description) if part]
    if not parts:
        return None
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = text[: max(limit - 1, 0)] + "…"
    return text


def _portfolio_keyboard(ids: list[int], current_id: int, admin_view: bool):
    """Клавиатура для просмотра одного фото портфолио. Листание — по кругу: после последнего
    фото ▶️ снова показывает первое, и наоборот для ◀️ у первого фото."""
    prev_id: int | None = None
    next_id: int | None = None
    if len(ids) > 1:
        idx = ids.index(current_id)
        prev_id = ids[idx - 1]  # idx=0 -> ids[-1], т.е. последний элемент — это и есть «по кругу»
        next_id = ids[(idx + 1) % len(ids)]

    if admin_view:
        return portfolio_admin_keyboard(current_id, prev_id, next_id)
    if prev_id is None:
        return None
    return portfolio_nav_keyboard(prev_id, next_id)


async def send_portfolio_view(message: Message, session: AsyncSession, admin_view: bool, tenant: TenantContext) -> None:
    """Показывает первое фото портфолио с клавиатурой листания (и, если admin_view — с кнопками
    управления). Используется и клиентским хендлером ниже, и мастерской веткой в admin/content.py."""
    items = await ContentRepository(session, tenant.master_id).get_active_portfolio()
    if not items:
        if admin_view:
            await message.answer("Портфолио пока пусто.", reply_markup=portfolio_admin_empty_keyboard())
        else:
            await message.answer("Портфолио пока пусто.")
        return
    item = items[0]
    keyboard = _portfolio_keyboard([i.id for i in items], item.id, admin_view)
    await message.answer_photo(item.telegram_file_id, caption=_portfolio_caption(item), reply_markup=keyboard)


@router.message(F.text == "📸 Портфолио")
async def show_portfolio(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await send_portfolio_view(message, session, admin_view=False, tenant=tenant)


@router.callback_query(PortfolioCallback.filter())
async def view_portfolio_item(
    callback: CallbackQuery, callback_data: PortfolioCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    items = await ContentRepository(session, tenant.master_id).get_active_portfolio()
    ids = [i.id for i in items]
    if callback_data.item_id not in ids:
        await callback.answer("Это фото больше недоступно.", show_alert=True)
        return
    item = next(i for i in items if i.id == callback_data.item_id)
    admin_view = await is_admin_mode(session, tenant, callback.from_user.id)
    keyboard = _portfolio_keyboard(ids, item.id, admin_view)
    media = InputMediaPhoto(media=item.telegram_file_id, caption=_portfolio_caption(item))
    await callback.message.edit_media(media=media, reply_markup=keyboard)
    await callback.answer()


@router.message(F.text == "✨ Обо мне")
async def show_about(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
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
    if profile is None or not profile.address:
        await message.answer("Адрес пока не указан.")
        return
    # Клиенту показываем только текст, который написал мастер (без эмодзи и прочего).
    await message.answer(escape(profile.address))


def _review_word(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "отзывов"
    if count % 10 == 1:
        return "отзыв"
    if 2 <= count % 10 <= 4:
        return "отзыва"
    return "отзывов"


def _rating_stars(average: float) -> str:
    # Округляем 0.5 всегда вверх (обычное математическое округление), а не «банкирское»
    # round() Python: иначе, например, среднее 2.5 показывало бы 2 звезды вместо 3.
    stars = max(1, min(5, math.floor(average + 0.5)))
    return "⭐" * stars


async def get_reviews_summary_text(session: AsyncSession, tenant: TenantContext) -> str:
    average, count = await ContentRepository(session, tenant.master_id).get_reviews_stats()
    if count == 0:
        return "Отзывов пока нет — будьте первым! 🙂"
    average_display = f"{average:.0f}" if average == int(average) else f"{average:.1f}"
    return f"{_rating_stars(average)} {average_display} 🗣️ {count} {_review_word(count)}"


async def send_reviews_entry(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    text = await get_reviews_summary_text(session, tenant=tenant)
    can_write = await _can_write_reviews(session, message.from_user.id, tenant=tenant)
    await message.answer(text, reply_markup=reviews_entry_keyboard(can_write))


def _format_reviews_block(reviews: list[Review]) -> str:
    return "\n\n".join(
        f"👤 {escape(r.author_name or 'Клиент')}\n{'⭐' * r.rating}\n{escape(r.text)}" for r in reviews
    )


async def _send_reviews_page(callback: CallbackQuery, session: AsyncSession, offset: int, edit: bool, tenant: TenantContext) -> None:
    repo = ContentRepository(session, tenant.master_id)
    reviews = await repo.get_reviews_page(offset, REVIEWS_PAGE_SIZE)
    if not reviews:
        await callback.answer("Отзывов пока нет." if offset == 0 else "Больше отзывов нет.", show_alert=True)
        return

    _, total = await repo.get_reviews_stats()
    has_more = offset + len(reviews) < total
    can_write = await _can_write_reviews(session, callback.from_user.id, tenant=tenant)
    keyboard = reviews_list_keyboard(has_more, offset + REVIEWS_PAGE_SIZE, can_write)
    text = _format_reviews_block(reviews)
    if edit:
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.message(F.text == "⭐ Отзывы")
async def show_reviews(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await send_reviews_entry(message, session, tenant=tenant)


@router.callback_query(ReviewsCallback.filter(F.action == "view"))
async def view_reviews(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    await _send_reviews_page(callback, session, offset=0, edit=False, tenant=tenant)


@router.callback_query(ReviewsPageCallback.filter())
async def view_more_reviews(
    callback: CallbackQuery, callback_data: ReviewsPageCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    await _send_reviews_page(callback, session, offset=max(callback_data.offset, 0), edit=True, tenant=tenant)


@router.callback_query(ReviewsCallback.filter(F.action == "add"))
async def start_add_review(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    # Мастер из админ-панели может оставлять сколько угодно отзывов — ограничение «один отзыв»
    # действует только на клиентов (и на мастера, если он зашёл как клиент).
    if await is_admin_mode(session, tenant, callback.from_user.id):
        await state.update_data(review_edit=False, review_as_admin=True)
        await callback.message.answer("Оцените, пожалуйста:", reply_markup=review_rating_keyboard())
        await callback.answer()
        return

    user = await UserRepository(session, tenant.master_id).get_or_create(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
    )
    denied = await _reviews_denied_text(session, tenant, user)
    if denied is not None:
        await callback.message.answer(denied)
        await callback.answer()
        return
    existing = await ContentRepository(session, tenant.master_id).get_review_by_client_id(user.id)
    if existing is not None:
        await callback.message.answer("Вы уже оставляли отзыв.", reply_markup=already_reviewed_keyboard())
        await callback.answer()
        return

    await state.update_data(review_edit=False, review_as_admin=False)
    await callback.message.answer("Оцените, пожалуйста:", reply_markup=review_rating_keyboard())
    await callback.answer()


@router.callback_query(ReviewsCallback.filter(F.action == "edit"))
async def start_edit_review(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(callback.from_user.id)
    denied = await _reviews_denied_text(session, tenant, user)
    if denied is not None:
        await callback.message.answer(denied)
        await callback.answer()
        return
    review = await ContentRepository(session, tenant.master_id).get_review_by_client_id(user.id) if user else None
    if review is None:
        await callback.answer("Отзыв не найден.", show_alert=True)
        return

    await state.update_data(review_edit=True, review_as_admin=False)
    await callback.message.edit_text("Оцените, пожалуйста:", reply_markup=review_rating_keyboard())
    await callback.answer()


@router.callback_query(ReviewsCallback.filter(F.action == "back"))
async def reviews_back_to_entry(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    await state.clear()
    text = await get_reviews_summary_text(session, tenant=tenant)
    can_write = await _can_write_reviews(session, callback.from_user.id, tenant=tenant)
    await callback.message.edit_text(text, reply_markup=reviews_entry_keyboard(can_write))
    await callback.answer()


@router.callback_query(ReviewRatingCallback.filter())
async def review_rating_selected(
    callback: CallbackQuery, callback_data: ReviewRatingCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    if not 1 <= callback_data.rating <= 5:  # подделанная кнопка: оценка вне диапазона (в БД — CHECK 1..5)
        await callback.answer("Эта кнопка устарела. Оцените заново.", show_alert=True)
        return
    # Клиента могли ограничить, пока открыта старая форма отзыва (мастер из админ-панели не ограничивается).
    if not (await state.get_data()).get("review_as_admin"):
        denied = await _reviews_denied_text(
            session, tenant, await UserRepository(session, tenant.master_id).get_by_telegram_id(callback.from_user.id)
        )
        if denied is not None:
            await state.clear()
            await callback.message.answer(denied)
            await callback.answer()
            return
    await state.update_data(rating=callback_data.rating)
    await state.set_state(ReviewFSM.enter_name)
    await callback.message.edit_text("Введите ваше имя:")
    await callback.answer()


@router.message(ReviewFSM.enter_name)
async def review_enter_name(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/") or text in _menu_button_texts():
        await state.clear()
        await message.answer("Отправка отзыва отменена. Нажмите «⭐ Отзывы» ещё раз, если передумаете.")
        return
    if not (await state.get_data()).get("review_as_admin"):
        denied = await _reviews_denied_text(
            session, tenant, await UserRepository(session, tenant.master_id).get_by_telegram_id(message.from_user.id)
        )
        if denied is not None:
            await state.clear()
            await message.answer(denied)
            return
    if not text or len(text) > MAX_AUTHOR_NAME_LEN:
        await message.answer(f"Введите имя текстом, не длиннее {MAX_AUTHOR_NAME_LEN} символов.")
        return

    await state.update_data(author_name=text)
    await state.set_state(ReviewFSM.enter_text)
    await message.answer("Напишите ваш отзыв:")


@router.message(ReviewFSM.enter_text)
async def review_enter_text(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/") or text in _menu_button_texts():
        await state.clear()
        await message.answer("Отправка отзыва отменена. Нажмите «⭐ Отзывы» ещё раз, если передумаете.")
        return
    if not (await state.get_data()).get("review_as_admin") and not (await tenant.get_settings()).allow_reviews:
        await state.clear()
        await message.answer(REVIEWS_DISABLED_TEXT)
        return
    if not text or len(text) > MAX_REVIEW_TEXT_LEN:
        await message.answer(f"Введите текст отзыва, не длиннее {MAX_REVIEW_TEXT_LEN} символов.")
        return

    data = await state.get_data()

    if data.get("review_as_admin") and is_owner(tenant.master, message.from_user.id):
        # Отзыв, добавленный мастером из админ-панели: без привязки к клиенту (client_id=NULL) и без
        # проверки «уже оставлял» — иначе второй отзыв упирался бы в лимит, а при переходе мастера
        # в клиентский режим эти отзывы считались бы его «единственным» клиентским отзывом.
        session.add(
            Review(
                master_id=tenant.master_id, author_name=data["author_name"], rating=data["rating"], text=text, active=True
            )
        )
        await session.commit()
        await state.clear()
        await message.answer("✅ Отзыв добавлен.", reply_markup=admin_reviews_menu_keyboard())
        return

    user = await UserRepository(session, tenant.master_id).get_or_create(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    denied = await _reviews_denied_text(session, tenant, user)
    if denied is not None:
        # Мастер ограничил клиента, пока тот заполнял форму отзыва.
        await state.clear()
        await message.answer(denied)
        return
    content_repo = ContentRepository(session, tenant.master_id)

    if data.get("review_edit"):
        review = await content_repo.get_review_by_client_id(user.id)
        if review is None:
            await state.clear()
            await message.answer("Отзыв не найден, попробуйте ещё раз через «⭐ Отзывы».")
            return
        review.rating = data["rating"]
        review.author_name = data["author_name"]
        review.text = text
    else:
        existing = await content_repo.get_review_by_client_id(user.id)
        if existing is not None:
            # На случай повторной отправки, пока форма уже была открыта раньше.
            await state.clear()
            await message.answer("Вы уже оставляли отзыв.", reply_markup=already_reviewed_keyboard())
            return
        review = Review(
            master_id=tenant.master_id,
            client_id=user.id,
            author_name=data["author_name"],
            rating=data["rating"],
            text=text,
            active=True,
        )
        session.add(review)

    await session.commit()
    await state.clear()
    await message.answer("Спасибо за отзыв! 💛", reply_markup=reviews_entry_keyboard())


@router.message(F.text == "📞 Связаться со мной")
async def show_contact(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    contact_username = await get_admin_contact_username(message.bot, tenant.master)
    if not contact_username:
        await message.answer("Контакт мастера пока не указан.")
        return
    await message.answer(f"📞 Написать мастеру: @{contact_username}")


@router.callback_query(NavCallback.filter(F.target == "back"))
async def stale_back_fallback(callback: CallbackQuery, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    """Подхватывает «⬅️ Назад» тогда, когда ни один из специфичных для конкретного шага
    мастера записи обработчиков не подошёл (этот роутер подключается последним, поэтому
    сюда попадают только колбэки, не пойманные раньше).

    Такое бывает, если состояние диалога (FSM, хранится в памяти процесса — MemoryStorage)
    было потеряно из-за рестарта бота, а старая inline-клавиатура осталась висеть в чате.
    Раньше кнопка в этом случае просто не отвечала (Telegram крутил "часики" до таймаута).
    Вместо этого возвращаем пользователя в его главное меню и просим начать заново.
    """
    await state.clear()
    await callback.answer("Сессия устарела, начните заново 🙂", show_alert=True)
    try:
        await callback.message.delete()
    except Exception:
        pass

    if await is_admin_mode(session, tenant, callback.from_user.id):
        await callback.message.answer("✨ Админ-панель мастера", reply_markup=admin_menu_keyboard())
        return

    tz = await get_master_tz(session, tenant.master_id)
    user = await UserRepository(session, tenant.master_id).get_by_telegram_id(callback.from_user.id)
    has_active_booking = False
    if user is not None:
        upcoming = await BookingRepository(session, tenant.master_id).get_user_upcoming(user.id, dt.datetime.now(tz))
        has_active_booking = len(upcoming) > 0

    await callback.message.answer(
        "Выберите действие",
        reply_markup=main_menu_keyboard(
            has_active_booking=has_active_booking, is_admin=is_owner(tenant.master, callback.from_user.id)
        ),
    )
