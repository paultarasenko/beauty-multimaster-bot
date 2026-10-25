"""Стандартные тексты «Подтверждение записи» и «Отмена записи».

Вынесены в функции, чтобы один и тот же текст использовался и при отправке клиенту, и на экране
«⚙️ Настройки → ✍️ Тексты сообщений» (там мастер видит, какой текст сейчас используется).
Название услуги экранируется под HTML (бот шлёт parse_mode=HTML); остальные аргументы — готовые строки.
"""

from html import escape


def default_confirmation_text(
    service_name: str, date_str: str, weekday: str, time_str: str, money_str: str, reminder_phrase: str | None
) -> str:
    text = (
        "✅ Запись подтверждена!\n\n"
        f"{escape(service_name)}\n\n"
        f"📅 {date_str} - {weekday}\n\n"
        f"🕐 {time_str}\n\n"
        f"💰 {money_str}"
    )
    # Напоминание о напоминании — только если мастер их не выключил.
    return text + (f"\n\nНапомним о ней {reminder_phrase} 🙂" if reminder_phrase else "")


def default_cancellation_text(service_name: str, date_str: str, weekday: str, time_str: str, money_str: str) -> str:
    return (
        "Запись отменена.\n\n"
        f"{escape(service_name)}\n"
        f"📅 {date_str} - {weekday}\n"
        f"🕐 {time_str}\n"
        f"💰 {money_str}"
    )
