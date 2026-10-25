"""Тексты напоминаний «за 2 часа» — клиенту и мастеру.

Вынесены в чистые функции без зависимостей от aiogram/БД, чтобы их можно было
тестировать отдельно от отправки.
"""

from html import escape

from app.utils.duration import format_duration
from app.utils.master_settings import DEFAULT_CURRENCY
from app.utils.money import format_money


def master_name_dative(full_name: str | None) -> str:
    """«к Анне», «к Марии», «к Юле» — имя мастера для фразы «запись к ...».

    Результат — обычный текст, не HTML: экранируется там, где вставляется в сообщение.

    Склоняются только распространённые женские имена (-а / -я / -ия). Для остальных
    окончаний имя автоматически не склонить, поэтому возвращаем нейтральную форму
    «мастеру Имя» — она всегда грамматически верна.
    """
    parts = (full_name or "").strip().split()
    first_name = parts[0] if parts else ""
    if not first_name:
        return "мастеру"
    if first_name.endswith("ия"):
        return first_name[:-2] + "ии"
    if first_name.endswith(("а", "я")):
        return first_name[:-1] + "е"
    return f"мастеру {first_name}"


def client_reminder_24h_text(
    service_name: str, time_str: str, price, address: str | None = None, *, currency: str = DEFAULT_CURRENCY
) -> str:
    """Напоминание клиенту за 24 часа: услуга, время, стоимость и (если задан) адрес мастера.

    Пользовательские поля экранируются: сообщение уходит как HTML."""
    text = (
        "👋 Напоминание о записи\n\n"
        "Завтра у вас запись:\n\n"
        f"{escape(service_name)}\n"
        f"🕐 {time_str}\n"
        f"💰 {format_money(price, currency)}\n"
    )
    if address:
        text += f"{escape(address)}\n"
    return text + "\nПожалуйста, подтвердите, что всё в силе."


def client_reminder_2h_text(
    master_name: str | None,
    service_name: str,
    time_str: str,
    price=None,
    address: str | None = None,
    *,
    currency: str = DEFAULT_CURRENCY,
) -> str:
    """Напоминание клиенту за 2 часа: услуга, время, стоимость и (если задан) адрес мастера.

    Бот шлёт сообщения в режиме HTML, поэтому всё пользовательское (услуга, адрес) экранируется:
    иначе «Брови & ресницы» ломает отправку, а упавшее напоминание не повторяется.
    Если цена не передана (price=None), строка со стоимостью не показывается."""
    text = f"🔔 Через 2 часа у вас запись\n\n{escape(service_name)}\n🕐 {time_str}"
    if price is not None:
        text += f"\n💰 {format_money(price, currency)}"
    if address:
        text += f"\n📍 {escape(address)}"
    return text


def master_reminder_2h_text(
    client_first_name: str | None,
    client_username: str | None,
    service_name: str,
    time_str: str,
    duration_minutes: int,
    price,
    manual_client_name: str | None = None,
    manual_client_contact: str | None = None,
    *,
    currency: str = DEFAULT_CURRENCY,
) -> str:
    """Напоминание мастеру за 2 часа.

    Эмодзи услуги (например «💎») хранится в самом названии услуги, поэтому
    отдельно перед ним ничего не добавляем. Пользовательские поля экранируются (сообщение уходит как HTML).

    manual_client_name/contact — данные, которые мастер сам ввёл при ручной записи для клиента, которого ещё нет
    в боте: у такой записи нет настоящего пользователя-клиента, поэтому показываем их, а не имя владельца записи.
    """
    if manual_client_name:
        client_line = escape(manual_client_name)
        if manual_client_contact:
            client_line += f" ({escape(manual_client_contact)})"
    elif client_username and client_first_name:
        client_line = f"{escape(client_first_name)} (@{escape(client_username)})"
    elif client_username:
        client_line = f"@{escape(client_username)}"
    elif client_first_name:
        client_line = f"{escape(client_first_name)} (нет username)"
    else:
        client_line = "нет username"
    return (
        f"🔔 Через 2 часа — запись на {time_str}\n"
        f"{escape(service_name)}\n"
        f"👤 {client_line}\n"
        f"⏱ {format_duration(duration_minutes)} · 💰 {format_money(price, currency)}"
    )
