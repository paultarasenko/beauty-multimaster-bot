from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.callback_data import ReminderActionCallback


def reminder_keyboard(booking_id: int, allow_reschedule: bool = True) -> InlineKeyboardMarkup:
    """allow_reschedule=False (мастер отключил перенос записей клиентом): кнопки «Перенести» нет."""
    second_row = []
    if allow_reschedule:
        second_row.append(
            InlineKeyboardButton(
                text="🔄 Перенести",
                callback_data=ReminderActionCallback(booking_id=booking_id, action="reschedule").pack(),
            )
        )
    second_row.append(
        InlineKeyboardButton(
            text="❌ Отменить",
            callback_data=ReminderActionCallback(booking_id=booking_id, action="cancel").pack(),
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Всё в силе",
                    callback_data=ReminderActionCallback(booking_id=booking_id, action="still_valid").pack(),
                )
            ],
            second_row,
        ]
    )
