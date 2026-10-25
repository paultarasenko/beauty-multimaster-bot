from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.admin.about import _menu_button_texts
from app.bot.keyboards.callback_data import AdminBufferCallback, AdminDurationCallback, AdminServiceCallback
from app.bot.menu_mode import is_admin_mode
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminServiceFSM
from app.database.models import Booking, Service
from app.database.repositories.service_repository import ServiceRepository
from app.utils.duration import format_duration
from app.utils.master_settings import DEFAULT_CURRENCY
from app.utils.money import format_money
from app.utils.service_input import (
    BUFFER_PRESETS,
    BUFFER_PROMPT,
    CLEAR_DESCRIPTION,
    DURATION_PRESETS,
    DURATION_PROMPT,
    InvalidServiceInput,
    parse_description,
    parse_duration,
    parse_name,
    parse_price,
)
from app.tenancy import TenantContext

router = Router(name="admin_services")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())



# --- Клавиатуры и тексты ---


def _btn(text: str, service_id: int, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=AdminServiceCallback(service_id=service_id, action=action).pack(),
    )


def _services_admin_keyboard(services: list[Service], currency: str = DEFAULT_CURRENCY) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"{'✅' if s.active else '🚫'} {s.name} — {format_money(s.price, currency)}", s.id, "open")] for s in services
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить услугу", callback_data="svc_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _service_card_text(service: Service, currency: str = DEFAULT_CURRENCY) -> str:
    status = "✅ Включена" if service.active else "🚫 Выключена"
    text = (
        f"{escape(service.name)}\n\n"
        f"💰 {format_money(service.price, currency)}\n"
        f"🕐 {format_duration(service.duration_minutes)}\n"
        f"Статус: {status}"
    )
    if service.buffer_minutes:
        text += f"\n⏳ Свободное время после услуги: {format_duration(service.buffer_minutes)}"
    if service.description:
        text += f"\n\n{escape(service.description)}"
    return text


def _service_card_keyboard(service: Service) -> InlineKeyboardMarkup:
    toggle_text = "🚫 Выключить" if service.active else "✅ Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(toggle_text, service.id, "toggle"), _btn("⏳ Свободное время", service.id, "buffer")],
            [_btn("✏️ Название", service.id, "rename"), _btn("💰 Цена", service.id, "price")],
            [_btn("📝 Описание", service.id, "description"), _btn("🕐 Длительность", service.id, "duration")],
            [_btn("⬅️ Назад", 0, "list"), _btn("🗑️ Удалить", service.id, "delete")],
        ]
    )


def _delete_confirm_keyboard(service_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("Да, удалить", service_id, "delete_yes"), _btn("⬅️ Назад", service_id, "open")]]
    )


def _cancel_edit_keyboard(service_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Отмена", service_id, "open")]])


def _duration_keyboard(service_id: int) -> InlineKeyboardMarkup:
    """Кнопки быстрого выбора длительности по 3 в ряд. service_id=0 — при добавлении новой услуги."""
    buttons = [
        InlineKeyboardButton(
            text=format_duration(minutes),
            callback_data=AdminDurationCallback(service_id=service_id, minutes=minutes).pack(),
        )
        for minutes in DURATION_PRESETS
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    if service_id:
        rows.append([_btn("⬅️ Отмена", service_id, "open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _buffer_keyboard(service_id: int) -> InlineKeyboardMarkup:
    """Выбор свободного времени: 0 сверху, затем пары 5/10 и 15/30, внизу «Назад» и 60."""

    def _minutes_btn(minutes: int) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=str(minutes), callback_data=AdminBufferCallback(service_id=service_id, minutes=minutes).pack()
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_minutes_btn(0)],
            [_minutes_btn(5), _minutes_btn(10)],
            [_minutes_btn(15), _minutes_btn(30)],
            [_btn("⬅️ Назад", service_id, "open"), _minutes_btn(60)],
        ]
    )


async def _cancel_if_menu_button(message: Message, state: FSMContext, text: str) -> bool:
    """Нажатие на кнопку меню или команда во время ввода — это выход из редактирования, а не новый текст."""
    if text.startswith("/") or text in _menu_button_texts():
        await state.clear()
        await message.answer("Редактирование отменено. Нажмите нужную кнопку ещё раз.")
        return True
    return False


async def _build_services_list(session: AsyncSession, prefix: str = "", *, tenant: TenantContext) -> tuple[str, InlineKeyboardMarkup]:
    services = await ServiceRepository(session, tenant.master_id).get_all()
    if not services:
        return f"{prefix}Услуг пока нет.", _services_admin_keyboard([])
    return (
        f"{prefix}💰 Управление услугами.\nНажмите на услугу, чтобы изменить её",
        _services_admin_keyboard(services, (await tenant.get_settings()).currency),
    )


async def _count_bookings(session: AsyncSession, service_id: int, tenant: TenantContext) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Booking)
        .where(Booking.master_id == tenant.master_id, Booking.service_id == service_id)
    )
    return result.scalar_one()


# --- Список услуг и карточка услуги ---


@router.message(F.text == "💰 Услуги и цены")
async def show_services_admin(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    # Если админ сейчас смотрит клиентское меню (кнопка «Профиль клиента»), ему нужен обычный
    # прайс, а не управление услугами — пропускаем хендлер дальше, к клиентскому show_price.
    if not await is_admin_mode(session, tenant, message.from_user.id):
        raise SkipHandler
    text, keyboard = await _build_services_list(session, tenant=tenant)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(AdminServiceCallback.filter(F.action == "list"))
async def back_to_services_list(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    text, keyboard = await _build_services_list(session, tenant=tenant)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AdminServiceCallback.filter(F.action == "open"))
async def open_service_card(
    callback: CallbackQuery, callback_data: AdminServiceCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    # Сюда же приходит кнопка «Отмена» при изменении названия/цены — сбрасываем ввод.
    await state.clear()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    await callback.message.edit_text(
        _service_card_text(service, (await tenant.get_settings()).currency), reply_markup=_service_card_keyboard(service)
    )
    await callback.answer()


@router.callback_query(AdminServiceCallback.filter(F.action == "toggle"))
async def toggle_service(callback: CallbackQuery, callback_data: AdminServiceCallback, session: AsyncSession, tenant: TenantContext) -> None:
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    # Деактивация вместо удаления — исторические bookings хранят snapshot и не ломаются,
    # но деактивированная услуга больше не показывается клиенту при записи.
    service.active = not service.active
    await session.commit()
    await callback.message.edit_text(
        _service_card_text(service, (await tenant.get_settings()).currency), reply_markup=_service_card_keyboard(service)
    )
    await callback.answer("Обновлено.")


# --- Свободное время после услуги ---


@router.callback_query(AdminServiceCallback.filter(F.action == "buffer"))
async def ask_service_buffer(callback: CallbackQuery, callback_data: AdminServiceCallback, session: AsyncSession, tenant: TenantContext) -> None:
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    await callback.message.edit_text(BUFFER_PROMPT, reply_markup=_buffer_keyboard(service.id))
    await callback.answer()


@router.callback_query(AdminBufferCallback.filter())
async def pick_service_buffer(
    callback: CallbackQuery, callback_data: AdminBufferCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    if callback_data.minutes not in BUFFER_PRESETS:
        await callback.answer()
        return
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    # Уже созданные записи хранят своё свободное время (snapshot) и не меняются:
    # новое значение действует только на новые записи и на переносы.
    service.buffer_minutes = callback_data.minutes
    await session.commit()
    await callback.message.edit_text(
        f"✅ Свободное время изменено.\n\n{_service_card_text(service, (await tenant.get_settings()).currency)}", reply_markup=_service_card_keyboard(service)
    )
    await callback.answer()


# --- Изменить название ---


@router.callback_query(AdminServiceCallback.filter(F.action == "rename"))
async def ask_new_name(
    callback: CallbackQuery, callback_data: AdminServiceCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    if await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id) is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    await state.set_state(AdminServiceFSM.edit_name)
    await state.update_data(service_id=callback_data.service_id)
    await callback.message.edit_text(
        "Введите новое название услуги:", reply_markup=_cancel_edit_keyboard(callback_data.service_id)
    )
    await callback.answer()


@router.message(AdminServiceFSM.edit_name)
async def save_service_name(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    if await _cancel_if_menu_button(message, state, (message.text or "").strip()):
        return
    try:
        name = parse_name(message.text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return

    data = await state.get_data()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(data["service_id"])
    if service is None:
        await state.clear()
        await message.answer("Услуга не найдена.")
        return

    service.name = name
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Название изменено.\n\n{_service_card_text(service, (await tenant.get_settings()).currency)}", reply_markup=_service_card_keyboard(service)
    )


# --- Изменить цену ---


@router.callback_query(AdminServiceCallback.filter(F.action == "price"))
async def ask_new_price(
    callback: CallbackQuery, callback_data: AdminServiceCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    if await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id) is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    await state.set_state(AdminServiceFSM.edit_price)
    await state.update_data(service_id=callback_data.service_id)
    await callback.message.edit_text(
        f"Введите новую цену (число, в {(await tenant.get_settings()).currency}):",
        reply_markup=_cancel_edit_keyboard(callback_data.service_id),
    )
    await callback.answer()


@router.message(AdminServiceFSM.edit_price)
async def save_service_price(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    try:
        price = parse_price(message.text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return

    data = await state.get_data()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(data["service_id"])
    if service is None:
        await state.clear()
        await message.answer("Услуга не найдена.")
        return

    service.price = price
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Цена изменена.\n\n{_service_card_text(service, (await tenant.get_settings()).currency)}", reply_markup=_service_card_keyboard(service)
    )


# --- Изменить описание ---


@router.callback_query(AdminServiceCallback.filter(F.action == "description"))
async def ask_new_description(
    callback: CallbackQuery, callback_data: AdminServiceCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    text = f"Введите новое описание услуги. Чтобы убрать описание — отправьте «{CLEAR_DESCRIPTION}»."
    if service.description:
        text += f"\n\nСейчас:\n{escape(service.description)}"
    await state.set_state(AdminServiceFSM.edit_description)
    await state.update_data(service_id=callback_data.service_id)
    await callback.message.edit_text(text, reply_markup=_cancel_edit_keyboard(callback_data.service_id))
    await callback.answer()


@router.message(AdminServiceFSM.edit_description)
async def save_service_description(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = (message.text or "").strip()
    if text and await _cancel_if_menu_button(message, state, text):
        return
    try:
        description = parse_description(text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return

    data = await state.get_data()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(data["service_id"])
    if service is None:
        await state.clear()
        await message.answer("Услуга не найдена.")
        return

    service.description = description
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Описание изменено.\n\n{_service_card_text(service, (await tenant.get_settings()).currency)}", reply_markup=_service_card_keyboard(service)
    )


# --- Изменить длительность ---


@router.callback_query(AdminServiceCallback.filter(F.action == "duration"))
async def ask_new_duration(
    callback: CallbackQuery, callback_data: AdminServiceCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    if await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id) is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    await state.set_state(AdminServiceFSM.edit_duration)
    await state.update_data(service_id=callback_data.service_id)
    await callback.message.edit_text(
        DURATION_PROMPT,
        reply_markup=_duration_keyboard(callback_data.service_id),
    )
    await callback.answer()


@router.callback_query(AdminDurationCallback.filter(F.service_id > 0))
async def pick_service_duration(
    callback: CallbackQuery, callback_data: AdminDurationCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    """Длительность выбрана кнопкой при изменении услуги."""
    if callback_data.minutes not in DURATION_PRESETS:
        await callback.answer()
        return
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    # Уже созданные записи хранят свою длительность (snapshot) и не меняются.
    service.duration_minutes = callback_data.minutes
    await session.commit()
    # Сбрасываем только ожидание длительности: нажатие на старую кнопку не должно ломать другой ввод.
    if await state.get_state() == AdminServiceFSM.edit_duration.state:
        await state.clear()
    await callback.message.edit_text(
        f"✅ Длительность изменена.\n\n{_service_card_text(service, (await tenant.get_settings()).currency)}", reply_markup=_service_card_keyboard(service)
    )
    await callback.answer()


@router.message(AdminServiceFSM.edit_duration)
async def save_service_duration(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    if await _cancel_if_menu_button(message, state, (message.text or "").strip()):
        return
    try:
        duration = parse_duration(message.text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return

    data = await state.get_data()
    service = await ServiceRepository(session, tenant.master_id).get_by_id(data["service_id"])
    if service is None:
        await state.clear()
        await message.answer("Услуга не найдена.")
        return

    # Уже созданные записи хранят свою длительность (snapshot) и не меняются.
    service.duration_minutes = duration
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Длительность изменена.\n\n{_service_card_text(service, (await tenant.get_settings()).currency)}", reply_markup=_service_card_keyboard(service)
    )


# --- Удалить услугу ---


@router.callback_query(AdminServiceCallback.filter(F.action == "delete"))
async def ask_delete_service(callback: CallbackQuery, callback_data: AdminServiceCallback, session: AsyncSession, tenant: TenantContext) -> None:
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    # В БД записи ссылаются на услугу (ON DELETE RESTRICT), поэтому услугу с записями удалить нельзя.
    bookings_count = await _count_bookings(session, service.id, tenant=tenant)
    if bookings_count > 0:
        await callback.answer(
            f"Нельзя удалить: по этой услуге есть записи ({bookings_count}). "
            "Выключите её — клиенты её больше не увидят.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        f"Удалить услугу «{escape(service.name)}» навсегда?\n\nЭто действие нельзя отменить.",
        reply_markup=_delete_confirm_keyboard(service.id),
    )
    await callback.answer()


@router.callback_query(AdminServiceCallback.filter(F.action == "delete_yes"))
async def confirm_delete_service(
    callback: CallbackQuery, callback_data: AdminServiceCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    service = await ServiceRepository(session, tenant.master_id).get_by_id(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    name = service.name
    if await _count_bookings(session, service.id, tenant=tenant) > 0:
        await callback.answer("Нельзя удалить: по этой услуге уже появились записи.", show_alert=True)
        return

    try:
        await session.delete(service)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await callback.answer("Нельзя удалить: услуга используется в записях.", show_alert=True)
        return

    text, keyboard = await _build_services_list(session, prefix=f"✅ Услуга «{escape(name)}» удалена.\n\n", tenant=tenant)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# --- Добавить услугу ---


@router.callback_query(F.data == "svc_add")
async def start_add_service(callback, state: FSMContext) -> None:
    await state.set_state(AdminServiceFSM.enter_name)
    await callback.message.answer("Введите название услуги:")
    await callback.answer()


@router.message(AdminServiceFSM.enter_name)
async def enter_service_name(message: Message, state: FSMContext, tenant: TenantContext) -> None:
    try:
        name = parse_name(message.text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return
    await state.update_data(name=name)
    await state.set_state(AdminServiceFSM.enter_price)
    await message.answer(f"Введите цену (число, в {(await tenant.get_settings()).currency}):")


@router.message(AdminServiceFSM.enter_price)
async def enter_service_price(message: Message, state: FSMContext) -> None:
    try:
        price = parse_price(message.text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return
    # Строкой, а не Decimal: состояние FSM должно переживать сериализацию (Redis-хранилище, JSON).
    await state.update_data(price=str(price))
    await state.set_state(AdminServiceFSM.enter_duration)
    await message.answer(DURATION_PROMPT, reply_markup=_duration_keyboard(0))


@router.callback_query(AdminDurationCallback.filter(F.service_id == 0), AdminServiceFSM.enter_duration)
async def pick_new_service_duration(
    callback: CallbackQuery, callback_data: AdminDurationCallback, state: FSMContext
) -> None:
    """Длительность выбрана кнопкой при добавлении услуги."""
    if callback_data.minutes not in DURATION_PRESETS:
        await callback.answer()
        return
    await state.update_data(duration=callback_data.minutes)
    await state.set_state(AdminServiceFSM.enter_description)
    await callback.message.edit_text(f"🕐 Длительность: {format_duration(callback_data.minutes)}")
    await callback.message.answer("Краткое описание услуги (или напишите 'нет'):")
    await callback.answer()


@router.callback_query(AdminDurationCallback.filter(F.service_id == 0))
async def stale_new_service_duration(callback: CallbackQuery) -> None:
    """Кнопка от уже завершённого или отменённого добавления услуги."""
    await callback.answer("Эта кнопка уже неактивна.")


@router.message(AdminServiceFSM.enter_duration)
async def enter_service_duration(message: Message, state: FSMContext) -> None:
    try:
        duration = parse_duration(message.text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return
    await state.update_data(duration=duration)
    await state.set_state(AdminServiceFSM.enter_description)
    await message.answer("Краткое описание услуги (или напишите 'нет'):")


@router.message(AdminServiceFSM.enter_description)
async def enter_service_description(
    message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    try:
        description = parse_description(message.text)
    except InvalidServiceInput as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()

    service = Service(
        master_id=tenant.master_id,
        name=data["name"],
        price=Decimal(data["price"]),
        duration_minutes=data["duration"],
        description=description,
        active=True,
        sort_order=0,
    )
    session.add(service)
    await session.commit()
    await message.answer(f"✅ Услуга «{escape(service.name)}» добавлена.")
    await state.clear()
