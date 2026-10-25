import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import (
    BTN_REMINDERS,
    reminder_card_keyboard,
    reminder_delete_confirm_keyboard,
    reminder_input_cancel_keyboard,
    reminders_empty_keyboard,
    reminders_list_keyboard,
    reminders_menu_keyboard,
)
from app.bot.keyboards.callback_data import AdminReminderCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.bot.states.booking_states import AdminReminderFSM
from app.database.models import AdminReminder, Recurrence
from app.database.repositories.admin_reminder_repository import AdminReminderRepository
from app.services.admin_reminder_service import AdminReminderService
from app.services.exceptions import ReminderInPastError
from app.utils.recurrence import (
    describe_schedule,
    format_moment,
    is_date_too_far,
    parse_date_input,
    parse_time_input,
)
from app.utils.timezone import format_date_ru, get_master_tz, to_local
from app.tenancy import TenantContext

router = Router(name="admin_reminders")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

MENU_TEXT = "🔔 Напоминания\n\nКак часто напоминать?"
MAX_TEXT_LEN = 1000  # лимит Telegram на сообщение — 4096; напоминание короткое, 1000 с запасом
MAX_LIST = 20  # кнопок в списке (лимит Telegram — 100 на клавиатуру)
MAX_LABEL_TEXT = 30  # сколько символов текста влезает в подпись кнопки списка
STALE = "Эта кнопка устарела. Откройте раздел заново."
# Подписи периодичности в списке «Мои напоминания».
RECURRENCE_LABELS = {
    Recurrence.ONCE.value: "Один раз",
    Recurrence.DAILY.value: "Каждый день",
    Recurrence.WEEKLY.value: "Каждую неделю",
    Recurrence.MONTHLY.value: "Каждый месяц",
    Recurrence.YEARLY.value: "Каждый год",
}


def _to_callback_message(callback: CallbackQuery) -> Message | None:
    """callback.message может быть недоступным (слишком старое сообщение) — тогда редактировать нечего."""
    return callback.message if isinstance(callback.message, Message) else None


def _card_text(reminder: AdminReminder, tz: dt.tzinfo) -> str:
    return (
        f"🔔 {escape(reminder.text)}\n\n"
        f"🗓 {describe_schedule(reminder.recurrence, reminder.anchor_date, reminder.remind_time)}\n"
        f"⏭ Ближайшее: {format_moment(reminder.next_fire_at, tz)}"
    )


def _button_label(reminder: AdminReminder, tz: dt.tzinfo) -> str:
    period = RECURRENCE_LABELS.get(reminder.recurrence, "")
    local = to_local(reminder.next_fire_at, tz)
    one_line = " ".join(reminder.text.split())
    if len(one_line) > MAX_LABEL_TEXT:
        one_line = one_line[:MAX_LABEL_TEXT] + "…"
    return f"{period} - {local.strftime('%d.%m / %H:%M')} - {one_line}"


async def _build_list(session: AsyncSession, tenant: TenantContext) -> tuple[str, InlineKeyboardMarkup]:
    tz = await get_master_tz(session, tenant.master_id)
    repo = AdminReminderRepository(session, tenant.master_id)
    reminders = await repo.list_upcoming(MAX_LIST)
    if not reminders:
        return "Напоминаний пока нет.", reminders_empty_keyboard()

    text = "Ваши напоминания. Нажмите на любое, чтобы посмотреть или удалить:"
    total = await repo.count()
    if total > len(reminders):
        text += f"\n\nПоказаны ближайшие {len(reminders)} из {total}."
    items = [(r.id, _button_label(r, tz)) for r in reminders]
    return text, reminders_list_keyboard(items)


# --- Меню и создание ---


@router.message(F.text == BTN_REMINDERS)
async def open_reminders_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(MENU_TEXT, reply_markup=reminders_menu_keyboard())


@router.callback_query(AdminReminderCallback.filter(F.action == "menu"))
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    message = _to_callback_message(callback)
    if message is not None:
        await message.edit_text(MENU_TEXT, reply_markup=reminders_menu_keyboard())
    await callback.answer()


@router.callback_query(AdminReminderCallback.filter(F.action == "new"))
async def start_new_reminder(
    callback: CallbackQuery, callback_data: AdminReminderCallback, state: FSMContext, session: AsyncSession, tenant: TenantContext
) -> None:
    message = _to_callback_message(callback)
    try:
        recurrence = Recurrence(callback_data.recurrence)
    except ValueError:
        await callback.answer(STALE, show_alert=True)
        return
    if message is None:
        await callback.answer(STALE, show_alert=True)
        return

    tomorrow = dt.datetime.now(await get_master_tz(session, tenant.master_id)).date() + dt.timedelta(days=1)
    await state.set_state(AdminReminderFSM.enter_date)
    await state.set_data({"recurrence": recurrence.value})
    await message.answer(
        "Введите дату.\n\n"
        f"Формат: день, месяц и год — ДДММГГ. Например, {tomorrow.strftime('%d%m%y')} — это "
        f"{format_date_ru(tomorrow)} {tomorrow.year}.",
        reply_markup=reminder_input_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(AdminReminderCallback.filter(F.action == "cancel"))
async def cancel_new_reminder(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    message = _to_callback_message(callback)
    if message is not None:
        await message.edit_text("Создание напоминания отменено.")
    await callback.answer()


@router.message(AdminReminderFSM.enter_date)
async def receive_date(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    anchor_date = parse_date_input(message.text or "")
    if anchor_date is None:
        await message.answer(
            "Не получилось разобрать дату. Введите её в формате ДДММГГ, например 231026.",
            reply_markup=reminder_input_cancel_keyboard(),
        )
        return

    today = dt.datetime.now(await get_master_tz(session, tenant.master_id)).date()
    if is_date_too_far(anchor_date, today):
        await message.answer("Введите дату корректно.", reply_markup=reminder_input_cancel_keyboard())
        return

    data = await state.get_data()
    if data.get("recurrence") == Recurrence.ONCE.value:
        if anchor_date < today:
            await message.answer(
                "Эта дата уже прошла. Введите дату, которая ещё не наступила.",
                reply_markup=reminder_input_cancel_keyboard(),
            )
            return

    await state.update_data(anchor_date=anchor_date.isoformat())
    await state.set_state(AdminReminderFSM.enter_time)
    await message.answer(
        "Введите время.\n\nФормат: часы и минуты — ЧЧММ. Например, 1930.",
        reply_markup=reminder_input_cancel_keyboard(),
    )


@router.message(AdminReminderFSM.enter_time)
async def receive_time(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    remind_time = parse_time_input(message.text or "")
    if remind_time is None:
        await message.answer(
            "Не получилось разобрать время. Введите его в формате ЧЧММ, например 1930.",
            reply_markup=reminder_input_cancel_keyboard(),
        )
        return

    data = await state.get_data()
    try:
        recurrence = Recurrence(data["recurrence"])
        anchor_date = dt.date.fromisoformat(data["anchor_date"])
    except (KeyError, ValueError):
        await state.clear()
        await message.answer("Что-то пошло не так. Нажмите «🔔 Напоминания» и начните заново.")
        return

    service = AdminReminderService(session, await get_master_tz(session, tenant.master_id), master_id=tenant.master_id)
    try:
        service.first_fire_at(recurrence, anchor_date, remind_time)
    except ReminderInPastError:
        await message.answer(
            "Это время уже прошло. Введите время, которое ещё не наступило.",
            reply_markup=reminder_input_cancel_keyboard(),
        )
        return

    await state.update_data(remind_time=remind_time.strftime("%H:%M"))
    await state.set_state(AdminReminderFSM.enter_text)
    await message.answer("Что напомнить? Отправьте текст напоминания.", reply_markup=reminder_input_cancel_keyboard())


@router.message(AdminReminderFSM.enter_text)
async def receive_text(message: Message, state: FSMContext, session: AsyncSession, tenant: TenantContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Отправьте текст напоминания сообщением.", reply_markup=reminder_input_cancel_keyboard()
        )
        return
    if len(text) > MAX_TEXT_LEN:
        await message.answer(
            f"Текст слишком длинный: {len(text)} символов, максимум {MAX_TEXT_LEN}. Сократите его и отправьте ещё раз.",
            reply_markup=reminder_input_cancel_keyboard(),
        )
        return

    data = await state.get_data()
    try:
        recurrence = Recurrence(data["recurrence"])
        anchor_date = dt.date.fromisoformat(data["anchor_date"])
        remind_time = dt.time.fromisoformat(data["remind_time"])
    except (KeyError, ValueError):
        await state.clear()
        await message.answer("Что-то пошло не так. Нажмите «🔔 Напоминания» и начните заново.")
        return

    tz = await get_master_tz(session, tenant.master_id)
    try:
        reminder = await AdminReminderService(session, tz, master_id=tenant.master_id).create(text, recurrence, anchor_date, remind_time)
    except ReminderInPastError:
        # Пока мастер писал текст, назначенное время успело наступить.
        await state.clear()
        await message.answer("Пока вы вводили текст, это время уже прошло. Создайте напоминание заново.")
        return

    await session.commit()
    await state.clear()
    await message.answer("✅ Напоминание создано.\n\n" + _card_text(reminder, tz))


# --- Просмотр и удаление ---


@router.callback_query(AdminReminderCallback.filter(F.action == "list"))
async def show_list(callback: CallbackQuery, session: AsyncSession, tenant: TenantContext) -> None:
    message = _to_callback_message(callback)
    if message is not None:
        text, keyboard = await _build_list(session, tenant=tenant)
        await message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AdminReminderCallback.filter(F.action == "open"))
async def open_reminder(callback: CallbackQuery, callback_data: AdminReminderCallback, session: AsyncSession, tenant: TenantContext) -> None:
    reminder = await AdminReminderRepository(session, tenant.master_id).get(callback_data.reminder_id)
    message = _to_callback_message(callback)
    if reminder is None or message is None:
        await callback.answer("Это напоминание уже удалено или сработало.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    await message.edit_text(_card_text(reminder, tz), reply_markup=reminder_card_keyboard(reminder.id))
    await callback.answer()


@router.callback_query(AdminReminderCallback.filter(F.action == "delete"))
async def ask_delete_confirmation(
    callback: CallbackQuery, callback_data: AdminReminderCallback, session: AsyncSession, tenant: TenantContext
) -> None:
    reminder = await AdminReminderRepository(session, tenant.master_id).get(callback_data.reminder_id)
    message = _to_callback_message(callback)
    if reminder is None or message is None:
        await callback.answer("Это напоминание уже удалено или сработало.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    await message.edit_text(
        "Удалить это напоминание?\n\n" + _card_text(reminder, tz),
        reply_markup=reminder_delete_confirm_keyboard(reminder.id),
    )
    await callback.answer()


@router.callback_query(AdminReminderCallback.filter(F.action == "confirm"))
async def confirm_delete(callback: CallbackQuery, callback_data: AdminReminderCallback, session: AsyncSession, tenant: TenantContext) -> None:
    deleted = await AdminReminderRepository(session, tenant.master_id).delete(callback_data.reminder_id)
    await session.commit()
    message = _to_callback_message(callback)
    if not deleted:
        await callback.answer("Это напоминание уже удалено или сработало.", show_alert=True)
        return
    if message is not None:
        await message.edit_text("✅ Напоминание удалено.")
    await callback.answer()