from functools import lru_cache
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from app.bot.keyboards.admin_keyboards import (
    admin_bookings_menu_keyboard,
    admin_info_menu_keyboard,
    admin_menu_keyboard,
    admin_stats_menu_keyboard,
)
from app.bot.keyboards.client_keyboards import info_menu_keyboard, main_menu_keyboard
from app.bot.states.booking_states import ContactMasterFSM, MasterReplyFSM, MasterRequestFSM
from app.services.limit_request_service import deliver_decision
from app.tenancy import is_owner


@lru_cache(maxsize=1)
def menu_button_texts() -> frozenset[str]:
    """Тексты кнопок всех меню бота. Новые кнопки подхватываются автоматически — они берутся из клавиатур."""
    keyboards = (
        admin_menu_keyboard(),
        admin_info_menu_keyboard(),
        admin_bookings_menu_keyboard(),
        admin_stats_menu_keyboard(),
        main_menu_keyboard(has_active_booking=False, is_admin=True),
        main_menu_keyboard(has_active_booking=True, is_admin=True),
        info_menu_keyboard(),
    )
    return frozenset(button.text for kb in keyboards for row in kb.keyboard for button in row)


class ResetInputOnMenuPressMiddleware(BaseMiddleware):
    """Если админ посреди ввода (название услуги, дата блокировки, время расписания и т.п.) нажал кнопку
    меню или отправил команду — незаконченный ввод сбрасывается ДО выбора обработчика.

    Без этого обработчик ввода видит текст кнопки как обычный ответ и сохраняет его (например, как название
    услуги), а кнопки, которые обрабатываются раньше него, оставляют ввод включённым — и следующее обычное
    сообщение улетает в него. После сброса кнопка срабатывает как обычно.

    Подключается как outer-middleware на dp.message, поэтому действует на все админские сценарии сразу.
    """

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        raw_state = data.get("raw_state")
        tenant = data.get("tenant")  # кладёт TenantMiddleware (update-уровень, раньше этого middleware)
        # Клиент, который пишет мастеру (запрос на запись сверх лимита), тоже может нажать кнопку меню посреди ввода.
        is_input_state = raw_state is not None and event.from_user is not None and (
            (tenant is not None and is_owner(tenant.master, event.from_user.id))
            or raw_state in (MasterRequestFSM.waiting_message.state, ContactMasterFSM.waiting_message.state)
        )
        if is_input_state:
            text = (event.text or "").strip()
            if text.startswith("/") or text in menu_button_texts():
                if raw_state == MasterReplyFSM.waiting_text.state:
                    # Мастер уже принял/отклонил запрос, но не дописал сообщение — решение всё равно уходит клиенту.
                    state_data = await data["state"].get_data()
                    request_id = state_data.get("request_id")
                    if request_id is not None and data.get("session") is not None and tenant is not None:
                        await deliver_decision(event.bot, data["session"], tenant.master_id, request_id, None)
                await data["state"].clear()
                # Фильтры состояний берут текущее состояние из data — обновляем и его, иначе обработчик
                # ввода всё равно сработает по старому значению.
                data["raw_state"] = None
        return await handler(event, data)
