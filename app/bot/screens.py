"""Чистый чат: удаление «мусора» от нажатий кнопок + синяя кнопка «Меню» (список команд).

Правило (одно на весь бот, для мастера и для клиента):

1. Нажатие кнопки нижнего меню (или команды из синей кнопки «Меню») — это «переход в другой раздел».
   Сообщение самого человека с названием кнопки («🕒 Проверить время») бот сразу удаляет.
2. Всё, что бот показал В ОТВЕТ на действия человека (экраны с инлайн-кнопками, календари, списки «На неделю»,
   «Выберите действие», настройки и т.п.), запоминается. При следующем переходе в другой раздел бот удаляет
   запомненное — в чате остаётся только актуальный экран.
3. Остаётся то, что бот присылает сам, без нажатия кнопки: уведомления о записях и отменах, напоминания,
   сообщения клиентов и карточки клиентов мастеру. Они отправляются не в ответ на апдейт этого чата
   (планировщик, другой чат) либо обёрнуты в `untracked()` (уведомления мастеру в MasterNotifier).
4. Текст, который человек вводит руками (название услуги, отзыв и т.п.), не трогается.

Страховка: сообщение, на котором сейчас висит нижняя клавиатура (последнее с ReplyKeyboardMarkup), не удаляется,
пока не появится новое такое сообщение. Так клавиатура не может пропасть из-за удаления её «носителя».

Запомненные id хранятся как у антиспама отключённых ботов (`disabled_replies.py`): Redis при FSM в Redis
(ключ `bbot:screens:<bot_id>:<chat_id>`, TTL чуть меньше 48 часов — старше Telegram всё равно не даёт удалить),
иначе память процесса. Любой сбой хранилища или удаления только пишется в лог и никогда не ломает обработку апдейта.
"""

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, BaseMiddleware
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
    Message,
    TelegramObject,
    Update,
)

from app.bot.keyboards.admin_keyboards import BTN_SETTINGS
from app.bot.keyboards.client_keyboards import phone_request_keyboard
from app.bot.screen_tracking import _Scope, _scope, ScreenStore
from app.tenancy import is_owner

logger = logging.getLogger(__name__)

DELETE_BATCH = 100
COMMANDS_RETRY_SECONDS = 600.0

# ---------- команды синей кнопки «Меню» ----------

# Команда → текст кнопки, которую она «нажимает». Для клиента и мастера списки разные (мастеру — свой список).
# /start и /admin уже есть в боте (client/start.py, admin/bookings.py), здесь только описания для списка.
CLIENT_COMMAND_BUTTONS: dict[str, str] = {
    "book": "📅 Записаться",
    "my": "📋 Мои записи",
    "check": "🕒 Проверить время",
    "contact": "📞 Связаться со мной",
}
ADMIN_COMMAND_BUTTONS: dict[str, str] = {
    "records": "📅 Записи",
    "schedule": "🕐 Расписание",
    "settings": BTN_SETTINGS,
}
NAV_COMMANDS: frozenset[str] = frozenset({"start", "admin", *CLIENT_COMMAND_BUTTONS, *ADMIN_COMMAND_BUTTONS})

CLIENT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="book", description="Записаться"),
    BotCommand(command="my", description="Мои записи"),
    BotCommand(command="check", description="Проверить время"),
    BotCommand(command="contact", description="Связаться с мастером"),
]
ADMIN_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="records", description="Записи"),
    BotCommand(command="schedule", description="Расписание"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="admin", description="Админ-панель"),
]


def command_name(text: str | None) -> str | None:
    """«/records@my_bot аргументы» → «records»; не команда → None."""
    if not text or not text.startswith("/"):
        return None
    head = text.split(maxsplit=1)[0][1:]
    return head.split("@", 1)[0].lower() or None


# ---------- что считается «переходом» ----------


def _echo_button_texts() -> frozenset[str]:
    """Кнопки нижней клавиатуры внутри сценария (не переход): их сообщение удаляется, экраны не трогаются."""
    return frozenset(b.text for row in phone_request_keyboard().keyboard for b in row if not b.request_contact)


def _is_navigation(message: Message) -> bool:
    from app.bot.middlewares.reset_input import menu_button_texts  # поздний импорт: избегаем цикла

    text = (message.text or "").strip()
    return text in menu_button_texts() or command_name(text) in NAV_COMMANDS


def _is_echo(message: Message) -> bool:
    if (message.text or "").strip() in _echo_button_texts():
        return True
    # Номер, отправленный кнопкой «📱 Да, поделиться номером» (contact от самого пользователя).
    return (
        message.contact is not None
        and message.from_user is not None
        and message.contact.user_id == message.from_user.id
    )


class ScreenCleanupMiddleware(BaseMiddleware):
    """update-middleware, подключается ПОСЛЕ TenantMiddleware (отключённым мастерам апдейты сюда не доходят)."""

    def __init__(self, store: ScreenStore) -> None:
        self._store = store
        self._commands_attempt: dict[tuple[int, int | None], float] = {}
        self._commands_done: set[tuple[int, int | None]] = set()

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        bot: Bot | None = data.get("bot")
        chat = data.get("event_chat")
        if not isinstance(event, Update) or bot is None or chat is None or chat.type != "private":
            return await handler(event, data)

        message = event.message
        navigation = message is not None and _is_navigation(message)
        echo = navigation or (message is not None and _is_echo(message))

        await self._ensure_commands(bot, data.get("tenant"))

        scope = _Scope(bot_id=bot.id, chat_id=chat.id)
        token = _scope.set(scope)
        succeeded = False
        try:
            result = await handler(event, data)
            succeeded = True
            return result
        finally:
            _scope.reset(token)
            if echo and message is not None:
                await self._delete(bot, chat.id, [message.message_id])
            try:
                # Старые экраны убираем только ПОСЛЕ того, как обработчик показал новый: человек не видит пустой чат.
                await self._commit(bot, chat.id, scope, prune=navigation and succeeded)
            except Exception:
                logger.warning("Экраны чата: не удалось обновить список", exc_info=True)

    async def _commit(self, bot: Bot, chat_id: int, scope: _Scope, *, prune: bool) -> None:
        """Сохраняет новые экраны апдейта; при переходе (prune) удаляет прежние.

        Удаление происходит только если обработчик что-то показал (scope.new) и не упал: иначе прежний экран остаётся.
        Носитель нижней клавиатуры сохраняется, пока обработчик не прислал новый.
        """
        old = await self._store.load(bot.id, chat_id)
        if not (prune and scope.new and old):
            if scope.new:
                await self._store.save(bot.id, chat_id, old + scope.new)
            return
        new_holds_keyboard = any(entry[1] for entry in scope.new)
        keep = None
        if not new_holds_keyboard:
            keep = next((i for i in range(len(old) - 1, -1, -1) if old[i][1]), None)
        to_delete = [e[0] for i, e in enumerate(old) if i != keep]
        kept = [old[keep]] if keep is not None else []
        await self._store.save(bot.id, chat_id, kept + scope.new)
        await self._delete(bot, chat_id, to_delete)

    @staticmethod
    async def _delete(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
        for start in range(0, len(message_ids), DELETE_BATCH):
            chunk = message_ids[start : start + DELETE_BATCH]
            try:
                await bot.delete_messages(chat_id, chunk)
            except Exception:
                # Часть сообщений могла уже быть удалена вручную: пробуем по одному, ошибки игнорируем.
                for message_id in chunk:
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id, message_id)

    async def _ensure_commands(self, bot: Bot, tenant: Any) -> None:
        """Список команд синей кнопки «Меню»: клиентский для всех, админский — только владельцу (его личный чат).

        Выставляется лениво, один раз за процесс на пару (бот, владелец): подхватывает и новых мастеров, и смену
        владельца без отдельных шагов в деплое. При сбое повтор не чаще раза в COMMANDS_RETRY_SECONDS."""
        owner_id = getattr(getattr(tenant, "master", None), "owner_telegram_id", None)
        key = (bot.id, owner_id)
        if key in self._commands_done:
            return
        now = time.monotonic()
        if now - self._commands_attempt.get(key, -COMMANDS_RETRY_SECONDS) < COMMANDS_RETRY_SECONDS:
            return
        self._commands_attempt[key] = now
        try:
            await bot.set_my_commands(CLIENT_COMMANDS, scope=BotCommandScopeDefault())
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            if owner_id is not None:
                await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
            self._commands_done.add(key)
        except Exception as exc:
            logger.warning("Команды меню: не удалось выставить (bot_id=%s): %s", bot.id, type(exc).__name__)


class CommandAsButtonMiddleware(BaseMiddleware):
    """message-outer-middleware: команда из синей кнопки «Меню» срабатывает как нажатие соответствующей кнопки.

    Подменяет текст сообщения на текст кнопки и передаёт дальше — обработчики кнопок работают без изменений.
    Админские команды выполняются только для владельца и сначала включают админ-панель (как кнопка «Профиль мастера»)."""

    async def __call__(
        self, handler: Callable[[Message, dict[str, Any]], Awaitable[Any]], event: Message, data: dict[str, Any]
    ) -> Any:
        name = command_name(event.text)
        tenant = data.get("tenant")
        if name in CLIENT_COMMAND_BUTTONS:
            button = CLIENT_COMMAND_BUTTONS[name]
        elif name in ADMIN_COMMAND_BUTTONS and tenant is not None and event.from_user is not None and is_owner(tenant.master, event.from_user.id):
            from app.bot.menu_mode import enter_admin_panel

            await enter_admin_panel(data["session"], tenant.master_id, event.from_user.id)
            button = ADMIN_COMMAND_BUTTONS[name]
        else:
            return await handler(event, data)
        return await handler(event.model_copy(update={"text": button, "entities": None}), data)
