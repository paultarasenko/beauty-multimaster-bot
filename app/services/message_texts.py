"""Данные для индивидуальных текстов мастера (этап 8): значения подстановок для записи."""

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Booking
from app.database.repositories.content_repository import ContentRepository
from app.utils.money import format_price
from app.utils.timezone import format_time_range, to_local, weekday_name_ru


async def profile_template_values(session: AsyncSession, master_id: int) -> tuple[str, str]:
    """(адрес, имя мастера) для подстановок {address} и {master_name}; пусто, если не заданы."""
    profile = await ContentRepository(session, master_id).get_master_profile()
    if profile is None:
        return "", ""
    return profile.address or "", profile.name or ""


def booking_template_values(
    booking: Booking,
    tz: dt.tzinfo,
    *,
    client_name: str | None,
    address: str = "",
    master_name: str = "",
    currency: str,
) -> dict[str, str]:
    """Значения подстановок шаблона для записи (экранирование делает движок шаблонов)."""
    start_local = to_local(booking.start_datetime, tz)
    return {
        "client_name": client_name or "",
        "service": booking.service_name_snapshot or "",
        "date": start_local.strftime("%d.%m.%Y"),
        "weekday": weekday_name_ru(start_local),
        "time": format_time_range(booking.start_datetime, booking.end_datetime, tz),
        "price": format_price(booking.service_price_snapshot),
        "currency": currency,
        "address": address,
        "master_name": master_name,
    }
