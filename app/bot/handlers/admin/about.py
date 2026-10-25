from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import (
    BTN_GREETING,
    admin_bookings_menu_keyboard,
    admin_info_menu_keyboard,
    admin_menu_keyboard,
)
from app.bot.keyboards.callback_data import AdminAboutCallback
from app.bot.keyboards.client_keyboards import info_menu_keyboard, main_menu_keyboard
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminAboutFSM
from app.database.models import MasterProfile
from app.database.repositories.content_repository import ContentRepository
from app.utils.greeting import DEFAULT_GREETING
from app.utils.reminder_texts import client_reminder_2h_text
from app.tenancy import TenantContext

router = Router(name="admin_about")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

# Редактируемые тексты: поле -> атрибут MasterProfile.
#   "description" — «Обо мне» (клиент видит в «Обо мне»)
#   "address"     — «Как меня найти»
#   "greeting"    — «Приветствие» (первое сообщение клиенту после /start)

FIELDS: dict[str, str] = {
    "description": "description",
    "address": "address",
    "greeting": "greeting_text",
}

CLEAR_VALUE = "-"  # этим символом можно очистить текст
MAX_NAME_LEN = 255  # столько помещается в колонку master_profile.name
MAX_ADDRESS_LEN = 500  # столько помещается в колонку master_profile.address
CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото
MESSAGE_LIMIT = 4000  # лимит Telegram на текст сообщения — 4096, оставляем запас
EMPTY_TEXT = "Текст пока не добавлен."
GREETING_HINT = (
    "Это ваше приветствие: именно его клиент увидит первым, когда нажмёт «Старт» и откроет бота."
)

# Пример услуги/времени для предпросмотра напоминания клиенту при смене/удалении имени мастера —
# фиксированные, реальные данные брать не нужно: это просто демонстрация формата фразы.
EXAMPLE_SERVICE_NAME = "Маникюр классический"
EXAMPLE_TIME = "15:00"


async def _send_name_preview(message: Message, profile: MasterProfile) -> None:
    """Показывает мастеру, как его имя будет выглядеть в клиентском напоминании «за 2 часа»."""
    example = client_reminder_2h_text(profile.name, EXAMPLE_SERVICE_NAME, EXAMPLE_TIME)
    await message.answer(f"Так будет выглядеть напоминание у клиента:\n\n{example}")


def _card_keyboard(field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Редактировать",
                    callback_data=AdminAboutCallback(action="edit", field=field).pack(),
                )
            ]
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отмена", callback_data=AdminAboutCallback(action="cancel", field="none").pack()
                )
            ]
        ]
    )


def _menu_button_texts() -> set[str]:
    """Тексты кнопок всех меню — чтобы нажатие на кнопку меню не сохранилось как новый текст."""
    keyboards = (
        admin_menu_keyboard(),
        admin_info_menu_keyboard(),
        admin_bookings_menu_keyboard(),
        main_menu_keyboard(has_active_booking=False, is_admin=True),
        info_menu_keyboard(),
    )
    return {button.text for kb in keyboards for row in kb.keyboard for button in row}


def _max_len(field: str, profile: MasterProfile) -> int:
    """Максимальная длина текста. Описание клиент видит подписью к фото (если фото есть) или сообщением."""
    if field == "address":
        return MAX_ADDRESS_LEN
    if field == "description":
        return CAPTION_LIMIT if profile.photo_file_id else MESSAGE_LIMIT
    return MESSAGE_LIMIT


async def send_editable_card(message: Message, profile: MasterProfile, field: str) -> None:
    """Показывает мастеру только его текст (без имени, эмодзи и прочего) и кнопку «Редактировать»."""
    raw = getattr(profile, FIELDS[field])
    if field == "greeting" and not raw:
        raw = DEFAULT_GREETING  # пока мастер не менял приветствие, клиент видит стандартный текст
    text = escape(raw) if raw else EMPTY_TEXT
    keyboard = _card_keyboard(field)

    if field == "description" and profile.photo_file_id:
        if len(raw or EMPTY_TEXT) <= CAPTION_LIMIT:
            await message.answer_photo(profile.photo_file_id, caption=text, reply_markup=keyboard)
        else:
            await message.answer_photo(profile.photo_file_id)
            await message.answer(text, reply_markup=keyboard)
        return
    await message.answer(text, reply_markup=keyboard)


def _about_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Обо мне",
                    callback_data=AdminAboutCallback(action="open", field="description").pack(),
                ),
                InlineKeyboardButton(
                    text="👤 Имя",
                    callback_data=AdminAboutCallback(action="open", field="name").pack(),
                ),
            ]
        ]
    )


@router.message(F.text == "✨ Обо мне")
async def show_about_me(message: Message) -> None:
    await message.answer("Что редактировать?", reply_markup=_about_choice_keyboard())


@router.callback_query(AdminAboutCallback.filter(F.action == "open"))
async def open_about_section(
    callback: CallbackQuery, callback_data: AdminAboutCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    if callback_data.field not in ("description", "name"):
        await callback.answer("Эта кнопка устарела. Откройте раздел заново.", show_alert=True)
        return
    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    if profile is None:
        await callback.answer("Профиль мастера не найден.", show_alert=True)
        return
    await callback.answer()
    if callback_data.field == "name":
        await send_name_card(callback.message, profile)
    else:
        await send_editable_card(callback.message, profile, "description")


@router.message(F.text == BTN_GREETING)
async def show_greeting(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    if profile is None:
        await message.answer("Профиль мастера не найден в базе данных.")
        return
    await message.answer(GREETING_HINT)
    await send_editable_card(message, profile, "greeting")


def _name_card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить",
                    callback_data=AdminAboutCallback(action="name_edit", field="none").pack(),
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=AdminAboutCallback(action="name_delete", field="none").pack(),
                ),
            ]
        ]
    )


async def send_name_card(message: Message, profile: MasterProfile) -> None:
    text = f"👤 Имя: {escape(profile.name)}" if profile.name else "👤 Имя не задано."
    await message.answer(text, reply_markup=_name_card_keyboard())


@router.callback_query(AdminAboutCallback.filter(F.action == "name_edit"))
async def ask_new_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminAboutFSM.edit_name)
    await callback.message.answer("Введите новое имя.", reply_markup=_cancel_keyboard())
    await callback.answer()


@router.callback_query(AdminAboutCallback.filter(F.action == "name_delete"))
async def delete_master_name(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    if profile is None:
        await callback.answer("Профиль мастера не найден.", show_alert=True)
        return
    profile.name = None
    await session.commit()
    await callback.answer()
    await callback.message.answer("Если имя удалено, клиенты будут видеть «к мастеру» без имени.")
    await _send_name_preview(callback.message, profile)
    await send_name_card(callback.message, profile)


@router.message(AdminAboutFSM.edit_name)
async def save_new_name(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте имя текстовым сообщением.")
        return

    # Нажатие на кнопку меню или команда — это не новое имя, а выход из редактирования.
    if text.startswith("/") or text in _menu_button_texts():
        await state.clear()
        await message.answer("Редактирование отменено. Нажмите нужную кнопку ещё раз.")
        return

    # Имя попадает в клиентские сообщения (режим HTML) — угловые скобки их сломают.
    if "<" in text or ">" in text:
        await message.answer("Символы < и > использовать нельзя. Отправьте имя без них.")
        return

    if len(text) > MAX_NAME_LEN:
        await message.answer(
            f"Имя слишком длинное: {len(text)} символов, максимум {MAX_NAME_LEN}. Сократите его и отправьте ещё раз."
        )
        return

    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    if profile is None:
        await state.clear()
        await message.answer("Профиль мастера не найден в базе данных.")
        return

    profile.name = text
    await session.commit()
    await state.clear()
    await message.answer("Сохранено.")
    await _send_name_preview(message, profile)
    await send_name_card(message, profile)


@router.callback_query(AdminAboutCallback.filter(F.action == "edit"))
async def ask_new_text(
    callback: CallbackQuery, callback_data: AdminAboutCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    field = callback_data.field
    if field not in FIELDS:
        # Кнопки от старой версии карточки («Имя», «Специализация» и т.д.)
        await callback.answer("Эта кнопка устарела. Откройте раздел заново.", show_alert=True)
        return
    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    if profile is None:
        await callback.answer("Профиль мастера не найден.", show_alert=True)
        return

    text = f"Введите новый текст. Чтобы убрать текст — отправьте «{CLEAR_VALUE}»."
    current = getattr(profile, FIELDS[field])
    if field == "greeting":
        text = f"Введите новый текст. Чтобы вернуть стандартное приветствие — отправьте «{CLEAR_VALUE}»."
        current = current or DEFAULT_GREETING
    if current:
        text += f"\n\nСейчас:\n{escape(current)}"

    await state.set_state(AdminAboutFSM.edit_field)
    await state.update_data(field=field)
    await callback.message.answer(text, reply_markup=_cancel_keyboard())
    await callback.answer()


@router.callback_query(AdminAboutCallback.filter(F.action == "cancel"))
async def cancel_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Редактирование отменено.")
    await callback.answer()


@router.message(AdminAboutFSM.edit_field)
async def save_new_text(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте новый текст сообщением.")
        return

    # Нажатие на кнопку меню или команда — это не новый текст, а выход из редактирования.
    if text.startswith("/") or text in _menu_button_texts():
        await state.clear()
        await message.answer("Редактирование отменено. Нажмите нужную кнопку ещё раз.")
        return

    data = await state.get_data()
    field = data.get("field")
    if field not in FIELDS:
        await state.clear()
        await message.answer("Что-то пошло не так, начните редактирование заново.")
        return

    profile = await ContentRepository(session, tenant.master_id).get_master_profile()
    if profile is None:
        await state.clear()
        await message.answer("Профиль мастера не найден в базе данных.")
        return

    # Клиентские сообщения отправляются в режиме HTML — угловые скобки их сломают.
    if "<" in text or ">" in text:
        await message.answer("Символы < и > использовать нельзя. Отправьте текст без них.")
        return

    value = None if text == CLEAR_VALUE else text
    if value is not None:
        limit = _max_len(field, profile)
        if len(value) > limit:
            await message.answer(
                f"Текст слишком длинный: {len(value)} символов, максимум {limit}"
                + (" (лимит Telegram для подписи к фото)." if field == "description" and profile.photo_file_id else ".")
                + " Сократите его и отправьте ещё раз."
            )
            return

    setattr(profile, FIELDS[field], value)
    await session.commit()
    await state.clear()
    await message.answer("Сохранено.")
    await send_editable_card(message, profile, field)