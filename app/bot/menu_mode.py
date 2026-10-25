"""Запоминает, в каком меню сейчас находится админ: в админ-панели или в клиентском режиме.

Кнопки «ℹ️ Информация», «⬅️ Назад» и некоторые другие выглядят одинаково и в админ-панели, и в
клиентском меню, а Telegram присылает боту только текст нажатой кнопки. Чтобы понять, какое
подменю показать и куда вернуть, бот помнит последнее показанное админу главное меню.

Раньше это хранилось в set() в памяти процесса — и слеталось при каждом рестарте/деплое бота.
Проблема была в том, что reply-клавиатура остаётся у админа на экране и после рестарта (Telegram
её не трогает), поэтому админ продолжал видеть кнопки админ-панели, но бот уже считал его в
клиентском режиме — и «Информация» открывала клиентское подменю вместо админского.

Теперь режим хранится в БД (master_clients.is_in_admin_panel — у связи человека с конкретным мастером) и переживает рестарт бота.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.user_repository import UserRepository
from app.tenancy import TenantContext, is_owner


async def enter_admin_panel(session: AsyncSession, master_id: int, telegram_id: int) -> None:
    await UserRepository(session, master_id).set_admin_panel_mode(telegram_id, True)


async def leave_admin_panel(session: AsyncSession, master_id: int, telegram_id: int) -> None:
    await UserRepository(session, master_id).set_admin_panel_mode(telegram_id, False)


async def is_in_admin_panel(session: AsyncSession, master_id: int, telegram_id: int) -> bool:
    return await UserRepository(session, master_id).is_in_admin_panel(telegram_id)


async def is_admin_mode(session: AsyncSession, tenant: TenantContext, telegram_id: int) -> bool:
    """Показывать ли этому человеку админскую версию меню/экрана: он ВЛАДЕЛЕЦ этого мастера И сейчас в админ-панели.

    Хендлеры, доступные и клиентам (прайс, портфолио, «Информация» и т.п.), должны спрашивать именно это, а не
    `is_in_admin_panel`: флаг в БД живёт у человека и переживает смену владельца мастера, а права определяет
    только `masters.owner_telegram_id`. Бывший владелец с «залипшим» флагом иначе увидел бы админские экраны.
    """
    return is_owner(tenant.master, telegram_id) and await is_in_admin_panel(session, tenant.master_id, telegram_id)
