"""Хендлеры суперадмин-бота (этап 9). Доступ — только SUPERADMIN_TELEGRAM_IDS (SuperadminAccessMiddleware на диспетчере).

Хендлеры не читают БД сами: списки и счётчики — `app/superadmin/service.py`, управление ботами — `app/multibot/management.py`
(та же логика, что у CLI). Каждое действие записывается в журнал. Токен бота нигде не сохраняется (ни в FSM, ни в логах, ни в ответах):
сообщение с токеном удаляется сразу, до подключения.
"""

import logging
import re
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.settings import settings
from app.multibot import management
from app.multibot.crypto import ConfigurationError, TokenCipher, TokenDecryptionError
from app.multibot.registry import BotRegistry
from app.reference import service as reference
from app.superadmin import service, trial
from app.superadmin.health import WebhookHealthMonitor
from app.superadmin.ui import (
    BTN_CREATE,
    BTN_JOURNAL,
    BTN_MASTERS,
    BTN_SUMMARY,
    PAGE_SIZE,
    SaCallback,
    SaFSM,
    _btn,
    cancel_keyboard,
    card_keyboard,
    card_text,
    check_keyboard,
    check_text,
    confirm_keyboard,
    journal_text,
    masters_keyboard,
    masters_text,
    menu_keyboard,
    more_keyboard,
    more_text,
    new_trial_keyboard,
    new_trial_text,
    overwrite_confirm,
    reset_confirm,
    search_keyboard,
    search_prompt_keyboard,
    search_prompt_text,
    search_text,
    summary_text,
)

logger = logging.getLogger(__name__)


TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
MAX_TELEGRAM_ID = 2**63 - 1
MAX_SEARCH_LENGTH = 64
MANAGEMENT_ERRORS = (management.ManagementError, ConfigurationError, TokenDecryptionError)
NO_MASTER = "Мастер не найден (возможно, удалён). Откройте список заново."


# ---- вспомогательное ----


async def _sync_registry(registry: BotRegistry | None, bot_id: int | None) -> None:
    """Применяет изменение сразу в реестре ботов этого процесса (иначе — через ~30 с по TTL кэша / на тике планировщика)."""
    if registry is None or bot_id is None:
        return
    try:
        await registry.refresh(bot_id)
    except Exception:
        logger.exception("Не удалось обновить реестр ботов после действия суперадмина (bot_id=%s)", bot_id)


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            await message.answer(text, reply_markup=markup)


def _webhook_states(rows: list[service.MasterRow], monitor: WebhookHealthMonitor | None) -> dict[int, str | None]:
    return {r.master_id: monitor.state_of(r.telegram_bot_id) if monitor else None for r in rows}


def _needs_decision(row: service.MasterRow, today) -> bool:
    """Подключённый мастер, у которого срок истёк или истекает завтра (так же отбирает ежедневная сводка)."""
    return row.is_active and trial.trial_status(row.trial_ends_on, today).needs_decision


async def _show_masters(
    target: Message | CallbackQuery, session_factory, monitor, page: int = 0, *, only_needs: bool = False
) -> None:
    all_rows = await service.list_masters(session_factory)
    today = trial.superadmin_today()
    needs_count = sum(1 for r in all_rows if _needs_decision(r, today))
    rows = [r for r in all_rows if _needs_decision(r, today)] if only_needs else all_rows
    page = max(0, min(page, max(0, (len(rows) - 1) // PAGE_SIZE)))
    text = masters_text(rows, page, _webhook_states(rows, monitor), only_needs=only_needs, today=today)
    markup = masters_keyboard(rows, page, only_needs=only_needs, needs_count=needs_count)
    if isinstance(target, CallbackQuery):
        await _show(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _show_card(
    target: Message | CallbackQuery, session_factory, monitor, master_id: int, notice: str | None = None
) -> None:
    card = await service.get_master_card(session_factory, master_id)
    if card is None:
        if isinstance(target, CallbackQuery):
            await target.answer(NO_MASTER, show_alert=True)
        else:
            await target.answer(NO_MASTER)
        return
    saved_at = await reference.reference_saved_at(session_factory) if master_id == reference.REFERENCE_MASTER_ID else None
    text = card_text(card, monitor.state_of(card.row.telegram_bot_id) if monitor else None, reference_saved_at=saved_at)
    if notice:
        text = f"{notice}\n\n{text}"
    markup = card_keyboard(card)
    if isinstance(target, CallbackQuery):
        await _show(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


def _back_to_card(master_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К карточке мастера", "card", master_id)]])


# ---- меню ----


def build_router() -> Router:
    """Новый роутер при каждом вызове: у aiogram-роутера может быть только один родитель, а build_runtime вызывается много раз."""
    router = Router(name="superadmin")
    router.message.filter(F.chat.type == "private")

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("🛠 Суперадмин-панель сервиса. Выберите действие.", reply_markup=menu_keyboard())


    @router.message(F.text == BTN_SUMMARY)
    async def show_summary(message: Message, state: FSMContext, session_factory: async_sessionmaker) -> None:
        await state.clear()
        await message.answer(summary_text(await service.get_summary(session_factory)))


    @router.message(F.text == BTN_MASTERS)
    async def show_masters(
        message: Message, state: FSMContext, session_factory: async_sessionmaker, health_monitor: WebhookHealthMonitor | None = None
    ) -> None:
        await state.clear()
        await _show_masters(message, session_factory, health_monitor)


    @router.message(F.text == BTN_JOURNAL)
    async def show_journal(message: Message, state: FSMContext, session_factory: async_sessionmaker) -> None:
        await state.clear()
        await message.answer(journal_text(await service.recent_actions(session_factory)))


    @router.message(F.text == BTN_CREATE)
    async def create_master_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(SaFSM.waiting_owner_id)
        await message.answer(
            "Пришлите Telegram id владельца нового мастера (число). Узнать id можно, например, у бота @userinfobot.",
            reply_markup=cancel_keyboard(),
        )


    # ---- ввод: id владельца ----


    @router.message(SaFSM.waiting_owner_id, F.text)
    async def create_master_finish(
        message: Message,
        state: FSMContext,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        raw = (message.text or "").strip()
        if not raw.isdigit() or not 0 < int(raw) <= MAX_TELEGRAM_ID:
            await message.answer("Нужно число — Telegram id владельца. Пришлите ещё раз или нажмите «Отмена».", reply_markup=cancel_keyboard())
            return
        # Мастер создаётся после выбора срока пробного периода (кнопки или своё число); id владельца ждёт в данных состояния.
        await state.set_state(SaFSM.waiting_new_trial)
        await state.update_data(owner_id=int(raw))
        await message.answer(new_trial_text(), reply_markup=new_trial_keyboard())


    async def _create_with_trial(
        target: Message | CallbackQuery, state: FSMContext, session_factory, monitor, actor: int, days: int | None
    ) -> None:
        """Создаёт мастера со сроком `days` дней от сегодня (None — без срока); владелец — из данных состояния."""
        owner_id = (await state.get_data()).get("owner_id")
        await state.clear()
        if owner_id is None:
            text = "Сессия создания устарела. Нажмите «➕ Создать мастера» заново."
            if isinstance(target, CallbackQuery):
                await target.answer(text, show_alert=True)
            else:
                await target.answer(text)
            return
        ends_on = trial.new_master_ends_on(days, trial.superadmin_today())
        master_id = await management.create_master(session_factory, owner_id, trial_ends_on=ends_on)
        term = f"срок до {ends_on:%d.%m.%Y}" if ends_on else "без срока"
        await service.log_action(
            session_factory, actor_telegram_id=actor, action="create_master", master_id=master_id, detail=f"владелец {owner_id}, {term}"
        )
        await _show_card(target, session_factory, monitor, master_id, notice=f"✅ Мастер №{master_id} создан ({term}; профиль и настройки по умолчанию).")


    @router.callback_query(SaCallback.filter(F.action == "trn"))
    async def create_master_trial_button(
        callback: CallbackQuery,
        callback_data: SaCallback,
        state: FSMContext,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await _create_with_trial(callback, state, session_factory, health_monitor, callback.from_user.id, callback_data.arg or None)
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "trnc"))
    async def create_master_trial_custom(callback: CallbackQuery) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(
                f"Пришлите число дней пробного периода (от 1 до {trial.MAX_TRIAL_DAYS}).", reply_markup=cancel_keyboard()
            )


    @router.message(SaFSM.waiting_new_trial, F.text)
    async def create_master_trial_number(
        message: Message, state: FSMContext, session_factory: async_sessionmaker, health_monitor: WebhookHealthMonitor | None = None
    ) -> None:
        days = trial.parse_days(message.text or "")
        if days is None:
            await message.answer(
                f"Нужно целое число дней от 1 до {trial.MAX_TRIAL_DAYS}. Пришлите ещё раз, выберите срок кнопкой выше или нажмите «Отмена».",
                reply_markup=cancel_keyboard(),
            )
            return
        await _create_with_trial(message, state, session_factory, health_monitor, message.from_user.id, days)


    # ---- ввод: токен бота ----


    @router.callback_query(SaCallback.filter(F.action == "connect"))
    async def connect_start(callback: CallbackQuery, callback_data: SaCallback, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(SaFSM.waiting_token)
        await state.update_data(master_id=callback_data.master_id)
        await _show(
            callback,
            f"Подключение бота мастеру №{callback_data.master_id}.\n\n"
            "Пришлите токен бота от @BotFather. Сообщение с токеном я удалю сразу; в журнал и логи токен не попадает.",
            cancel_keyboard(callback_data.master_id),
        )
        await callback.answer()


    @router.message(SaFSM.waiting_token)
    async def connect_finish(
        message: Message,
        state: FSMContext,
        session_factory: async_sessionmaker,
        registry: BotRegistry | None = None,
        health_monitor: WebhookHealthMonitor | None = None,
        sa_bot_id: int = 0,
    ) -> None:
        token = (message.text or "").strip()
        # Первое действие — удалить сообщение (в нём может быть токен), и только потом всё остальное.
        try:
            await message.delete()
            deleted = True
        except Exception:
            deleted = False
        warn = "" if deleted else "⚠️ Не удалось удалить сообщение с токеном — удалите его вручную.\n\n"

        master_id = (await state.get_data()).get("master_id")
        if master_id is None:
            await state.clear()
            await message.answer(f"{warn}Сессия подключения устарела. Откройте карточку мастера заново.")
            return
        if not token or not TOKEN_RE.match(token):
            await message.answer(
                f"{warn}Это не похоже на токен бота (формат «123456:ABC…»). Пришлите токен текстом ещё раз или нажмите «Отмена».",
                reply_markup=cancel_keyboard(master_id),
            )
            return
        if int(token.split(":", 1)[0]) == sa_bot_id:
            await message.answer(
                f"{warn}Это токен самого суперадмин-бота — его нельзя подключить мастеру. Пришлите токен бота мастера.",
                reply_markup=cancel_keyboard(master_id),
            )
            return

        await state.clear()
        actor = message.from_user.id
        try:
            row = await management.add_bot(
                session_factory,
                TokenCipher.from_settings(),
                master_id=master_id,
                token=token,
                public_base_url=settings.public_base_url,
            )
        except MANAGEMENT_ERRORS as exc:
            await service.log_action(session_factory, actor_telegram_id=actor, action="add_bot", master_id=master_id, ok=False, detail=str(exc))
            await message.answer(f"{warn}❌ Не удалось подключить бота: {escape(str(exc))}", reply_markup=_back_to_card(master_id))
            return
        finally:
            token = ""  # токен дальше не нужен
        await service.log_action(
            session_factory, actor_telegram_id=actor, action="add_bot", master_id=master_id, bot_id=row.telegram_bot_id, detail=f"@{row.bot_username}"
        )
        await _sync_registry(registry, row.telegram_bot_id)
        await _show_card(message, session_factory, health_monitor, master_id, notice=f"{warn}✅ Бот @{escape(row.bot_username or '')} подключён, webhook установлен.")


    # ---- карточка и список ----


    @router.callback_query(SaCallback.filter(F.action == "cancel"))
    async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _show(callback, "Отменено.")
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "list"))
    async def open_list(
        callback: CallbackQuery,
        callback_data: SaCallback,
        state: FSMContext,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await state.clear()
        await _show_masters(callback, session_factory, health_monitor, callback_data.arg)
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "card"))
    async def open_card(
        callback: CallbackQuery,
        callback_data: SaCallback,
        state: FSMContext,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await state.clear()
        await _show_card(callback, session_factory, health_monitor, callback_data.master_id)
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "more"))
    async def open_more(callback: CallbackQuery, callback_data: SaCallback, session_factory: async_sessionmaker) -> None:
        card = await service.get_master_card(session_factory, callback_data.master_id)
        if card is None:
            await callback.answer(NO_MASTER, show_alert=True)
            return
        await _show(callback, more_text(card), more_keyboard(card))
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "mj"))
    async def open_master_journal(callback: CallbackQuery, callback_data: SaCallback, session_factory: async_sessionmaker) -> None:
        master_id = callback_data.master_id
        records = await service.recent_actions(session_factory, master_id=master_id)
        await _show(callback, journal_text(records, master_id=master_id), _back_to_card(master_id))
        await callback.answer()


    # ---- поиск мастера ----


    @router.callback_query(SaCallback.filter(F.action == "find"))
    async def find_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(SaFSM.waiting_search)
        await _show(callback, search_prompt_text(), search_prompt_keyboard())
        await callback.answer()


    @router.message(SaFSM.waiting_search, F.text)
    async def find_finish(
        message: Message, state: FSMContext, session_factory: async_sessionmaker, health_monitor: WebhookHealthMonitor | None = None
    ) -> None:
        query = (message.text or "").strip()
        if not query or len(query) > MAX_SEARCH_LENGTH:
            await message.answer(
                f"Пришлите Telegram id владельца, номер мастера или @username бота (до {MAX_SEARCH_LENGTH} символов).",
                reply_markup=search_prompt_keyboard(),
            )
            return
        await state.clear()
        rows = service.match_masters(await service.list_masters(session_factory), query)
        await message.answer(search_text(query, rows, _webhook_states(rows, health_monitor)), reply_markup=search_keyboard(rows))


    # ---- срок пробного периода существующего мастера ----


    @router.callback_query(SaCallback.filter(F.action == "need"))
    async def open_needs_list(
        callback: CallbackQuery,
        callback_data: SaCallback,
        state: FSMContext,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await state.clear()
        await _show_masters(callback, session_factory, health_monitor, callback_data.arg, only_needs=True)
        await callback.answer()


    async def _extend(
        target: Message | CallbackQuery, session_factory, monitor, actor: int, master_id: int, days: int
    ) -> None:
        new_end = await service.extend_trial(session_factory, master_id, days, trial.superadmin_today())
        if new_end is None:
            if isinstance(target, CallbackQuery):
                await target.answer(NO_MASTER, show_alert=True)
            else:
                await target.answer(NO_MASTER)
            return
        await service.log_action(
            session_factory, actor_telegram_id=actor, action="extend_trial", master_id=master_id,
            detail=f"+{days} дн., срок до {new_end:%d.%m.%Y}",
        )
        await _show_card(target, session_factory, monitor, master_id, notice=f"✅ Срок продлён до {new_end:%d.%m.%Y}.")


    @router.callback_query(SaCallback.filter(F.action == "ext"))
    async def extend_button(
        callback: CallbackQuery,
        callback_data: SaCallback,
        state: FSMContext,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await state.clear()
        if not 1 <= callback_data.arg <= trial.MAX_TRIAL_DAYS:
            await callback.answer("Некорректное число дней.", show_alert=True)
            return
        await _extend(callback, session_factory, health_monitor, callback.from_user.id, callback_data.master_id, callback_data.arg)
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "extc"))
    async def extend_custom_start(callback: CallbackQuery, callback_data: SaCallback, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(SaFSM.waiting_extend_days)
        await state.update_data(master_id=callback_data.master_id)
        await _show(
            callback,
            f"На сколько дней продлить срок мастера №{callback_data.master_id}? Пришлите число от 1 до {trial.MAX_TRIAL_DAYS}.\n\n"
            "Отсчёт — от текущего окончания срока, а если срок уже истёк или не задан — от сегодняшней даты.",
            cancel_keyboard(callback_data.master_id),
        )
        await callback.answer()


    @router.message(SaFSM.waiting_extend_days, F.text)
    async def extend_custom_finish(
        message: Message, state: FSMContext, session_factory: async_sessionmaker, health_monitor: WebhookHealthMonitor | None = None
    ) -> None:
        master_id = (await state.get_data()).get("master_id")
        if master_id is None:
            await state.clear()
            await message.answer("Сессия устарела. Откройте карточку мастера заново.")
            return
        days = trial.parse_days(message.text or "")
        if days is None:
            await message.answer(
                f"Нужно целое число дней от 1 до {trial.MAX_TRIAL_DAYS}. Пришлите ещё раз или нажмите «Отмена».",
                reply_markup=cancel_keyboard(master_id),
            )
            return
        await state.clear()
        await _extend(message, session_factory, health_monitor, message.from_user.id, master_id, days)


    @router.callback_query(SaCallback.filter(F.action == "chown"))
    async def change_owner_start(callback: CallbackQuery, callback_data: SaCallback, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(SaFSM.waiting_new_owner)
        await state.update_data(master_id=callback_data.master_id)
        await _show(
            callback,
            f"Пришлите Telegram id нового владельца мастера №{callback_data.master_id} (число). "
            "Узнать id можно у бота @userinfobot.\n\n"
            "Админ-панель бота этого мастера будет открываться только с этого аккаунта.",
            cancel_keyboard(callback_data.master_id),
        )
        await callback.answer()


    @router.message(SaFSM.waiting_new_owner, F.text)
    async def change_owner_finish(
        message: Message, state: FSMContext, session_factory: async_sessionmaker, health_monitor: WebhookHealthMonitor | None = None
    ) -> None:
        master_id = (await state.get_data()).get("master_id")
        if master_id is None:
            await state.clear()
            await message.answer("Сессия устарела. Откройте карточку мастера заново.")
            return
        raw = (message.text or "").strip()
        if not raw.isdigit() or not 0 < int(raw) <= MAX_TELEGRAM_ID:
            await message.answer(
                "Нужно число — Telegram id владельца. Пришлите ещё раз или нажмите «Отмена».", reply_markup=cancel_keyboard(master_id)
            )
            return
        await state.clear()
        owner_id = int(raw)
        found, previous = await service.set_owner_telegram_id(session_factory, master_id, owner_id)
        if not found:
            await message.answer(NO_MASTER)
            return
        await service.log_action(
            session_factory, actor_telegram_id=message.from_user.id, action="set_owner", master_id=master_id,
            detail=f"владелец {previous or '—'} → {owner_id}",
        )
        await _show_card(
            message, session_factory, health_monitor, master_id, notice=f"✅ Владелец изменён: {previous or '—'} → {owner_id}."
        )


    @router.callback_query(SaCallback.filter(F.action == "notrial"))
    async def clear_trial(
        callback: CallbackQuery,
        callback_data: SaCallback,
        state: FSMContext,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await state.clear()
        master_id = callback_data.master_id
        if not await service.set_trial_ends_on(session_factory, master_id, None):
            await callback.answer(NO_MASTER, show_alert=True)
            return
        await service.log_action(
            session_factory, actor_telegram_id=callback.from_user.id, action="clear_trial", master_id=master_id, detail="без срока"
        )
        await _show_card(callback, session_factory, health_monitor, master_id, notice="✅ Срок снят: мастер остаётся подключённым без срока.")
        await callback.answer()


    # ---- проверка и webhook ----


    @router.callback_query(SaCallback.filter(F.action == "check"))
    async def check_connection(callback: CallbackQuery, callback_data: SaCallback, session_factory: async_sessionmaker) -> None:
        master_id = callback_data.master_id
        await callback.answer("Проверяю…")
        try:
            check = await management.check_bot(
                session_factory, TokenCipher.from_settings(), master_id=master_id, public_base_url=settings.public_base_url
            )
        except MANAGEMENT_ERRORS as exc:
            await service.log_action(session_factory, actor_telegram_id=callback.from_user.id, action="check_bot", master_id=master_id, ok=False, detail=str(exc))
            await _show(callback, f"❌ Проверка не выполнена: {escape(str(exc))}", _back_to_card(master_id))
            return
        ok = check.token_valid is True and check.error is None and check.webhook_ok is not False
        summary = "ok" if ok else (check.error or ("webhook не в порядке" if check.token_valid else "токен недействителен"))
        await service.log_action(
            session_factory, actor_telegram_id=callback.from_user.id, action="check_bot", master_id=master_id,
            bot_id=check.telegram_bot_id, ok=ok, detail=summary,
        )
        await _show(callback, check_text(check), check_keyboard(check))


    async def _refresh_webhook(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        registry: BotRegistry | None,
        monitor: WebhookHealthMonitor | None,
        *,
        rotate: bool,
    ) -> None:
        master_id = callback_data.master_id
        action = "rotate_webhook_secret" if rotate else "refresh_webhook"
        await callback.answer("Регистрирую webhook…")
        try:
            row = await management.refresh_webhook(
                session_factory,
                TokenCipher.from_settings(),
                master_id=master_id,
                public_base_url=settings.public_base_url,
                rotate_secret=rotate,
            )
        except MANAGEMENT_ERRORS as exc:
            await service.log_action(session_factory, actor_telegram_id=callback.from_user.id, action=action, master_id=master_id, ok=False, detail=str(exc))
            await _show(callback, f"❌ Webhook не перерегистрирован: {escape(str(exc))}", _back_to_card(master_id))
            return
        await service.log_action(
            session_factory, actor_telegram_id=callback.from_user.id, action=action, master_id=master_id, bot_id=row.telegram_bot_id
        )
        await _sync_registry(registry, row.telegram_bot_id)
        notice = "✅ Webhook перерегистрирован" + (", секрет заменён." if rotate else ".")
        await _show_card(callback, session_factory, monitor, master_id, notice=notice)


    @router.callback_query(SaCallback.filter(F.action == "wh"))
    async def refresh_webhook(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        registry: BotRegistry | None = None,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await _refresh_webhook(callback, callback_data, session_factory, registry, health_monitor, rotate=False)


    @router.callback_query(SaCallback.filter(F.action == "whrot"))
    async def rotate_webhook(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        registry: BotRegistry | None = None,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        await _refresh_webhook(callback, callback_data, session_factory, registry, health_monitor, rotate=True)


    # ---- отключение / включение мастера и бота, удаление бота ----

    CONFIRM_TEXTS = {
        "ask_md": (
            "Отключить мастера №{id}?\n\nБот перестанет работать: клиентам он ответит, что запись у мастера временно недоступна, "
            "мастеру — что бот отключён. Напоминания отправляться не будут. "
            "Данные мастера не удаляются; включить обратно можно в любой момент.",
            "Да, отключить",
            "do_md",
        ),
        "ask_rm": (
            "Удалить бота мастера №{id}?\n\nWebhook у Telegram будет снят, токен удалён из базы, незаконченные диалоги бота очищены. "
            "Данные мастера (записи, клиенты) не удаляются. Чтобы вернуть бота, его нужно подключить заново по токену.",
            "Да, удалить бота",
            "do_rm",
        ),
    }


    @router.callback_query(SaCallback.filter(F.action.in_(set(CONFIRM_TEXTS))))
    async def ask_confirm(callback: CallbackQuery, callback_data: SaCallback) -> None:
        text, yes_text, yes_action = CONFIRM_TEXTS[callback_data.action]
        await _show(callback, text.format(id=callback_data.master_id, ref=reference.REFERENCE_MASTER_ID), confirm_keyboard(yes_text, yes_action, callback_data.master_id))
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "ask_rs"))
    async def ask_reset(callback: CallbackQuery, callback_data: SaCallback, session_factory: async_sessionmaker) -> None:
        card = await service.get_master_card(session_factory, callback_data.master_id)
        if card is None:
            await callback.answer(NO_MASTER, show_alert=True)
            return
        text, markup = reset_confirm(card, await reference.reference_saved_at(session_factory))
        await _show(callback, text, markup)
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "ask_ow"))
    async def ask_overwrite(callback: CallbackQuery, callback_data: SaCallback, session_factory: async_sessionmaker) -> None:
        text, markup = overwrite_confirm(callback_data.master_id, await reference.reference_saved_at(session_factory))
        await _show(callback, text, markup)
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action.in_({"do_md", "en_m"})))
    async def toggle_master(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        registry: BotRegistry | None = None,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        master_id, active = callback_data.master_id, callback_data.action == "en_m"
        card = await service.get_master_card(session_factory, master_id)
        if card is None or not await service.set_master_active(session_factory, master_id, active):
            await callback.answer(NO_MASTER, show_alert=True)
            return
        await service.log_action(
            session_factory, actor_telegram_id=callback.from_user.id, action="enable_master" if active else "disable_master",
            master_id=master_id, bot_id=card.row.telegram_bot_id,
        )
        await _sync_registry(registry, card.row.telegram_bot_id)
        await _show_card(callback, session_factory, health_monitor, master_id, notice="✅ Мастер включён." if active else "✅ Мастер отключён.")
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "en_b"))
    async def enable_bot(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        registry: BotRegistry | None = None,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        master_id = callback_data.master_id
        bot_id = await service.set_bot_active(session_factory, master_id, True)
        if bot_id is None:
            await callback.answer("У мастера нет бота.", show_alert=True)
            return
        await service.log_action(session_factory, actor_telegram_id=callback.from_user.id, action="enable_bot", master_id=master_id, bot_id=bot_id)
        await _sync_registry(registry, bot_id)
        await _show_card(callback, session_factory, health_monitor, master_id, notice="✅ Бот включён.")
        await callback.answer()


    @router.callback_query(SaCallback.filter(F.action == "do_rm"))
    async def remove_bot(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        registry: BotRegistry | None = None,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        master_id = callback_data.master_id
        card = await service.get_master_card(session_factory, master_id)
        bot_id = card.row.telegram_bot_id if card else None
        await callback.answer("Удаляю бота…")
        try:
            warning = await management.remove_bot(session_factory, TokenCipher.from_settings(), master_id=master_id)
        except MANAGEMENT_ERRORS as exc:
            await service.log_action(session_factory, actor_telegram_id=callback.from_user.id, action="remove_bot", master_id=master_id, bot_id=bot_id, ok=False, detail=str(exc))
            await _show(callback, f"❌ Бот не удалён: {escape(str(exc))}", _back_to_card(master_id))
            return
        await service.log_action(
            session_factory, actor_telegram_id=callback.from_user.id, action="remove_bot", master_id=master_id, bot_id=bot_id, detail=warning
        )
        await _sync_registry(registry, bot_id)
        notice = "✅ Бот удалён; данные мастера сохранены." + (f"\n⚠️ {escape(warning)}" if warning else "")
        await _show_card(callback, session_factory, health_monitor, master_id, notice=notice)


    @router.callback_query(SaCallback.filter(F.action == "do_rs"))
    async def reset_to_reference(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        master_id = callback_data.master_id
        card = await service.get_master_card(session_factory, master_id)
        bot_id = card.row.telegram_bot_id if card else None
        await callback.answer("Сбрасываю к эталону…")
        try:
            result = await reference.reset_master_to_reference(
                session_factory, TokenCipher.from_settings(), master_id=master_id, upload_chat_id=callback.from_user.id
            )
        except (reference.ReferenceResetError, *MANAGEMENT_ERRORS) as exc:
            await service.log_action(
                session_factory, actor_telegram_id=callback.from_user.id, action="reset_reference", master_id=master_id,
                bot_id=bot_id, ok=False, detail=str(exc),
            )
            await _show(callback, f"❌ Сброс не выполнен, данные мастера не менялись: {escape(str(exc))}", _back_to_card(master_id))
            return
        await service.log_action(
            session_factory, actor_telegram_id=callback.from_user.id, action="reset_reference", master_id=master_id,
            bot_id=bot_id, detail="; ".join(w for w in (result.avatar_warning, result.fsm_warning) if w) or None,
        )
        warnings = [f"Фото бота: {escape(result.avatar_warning)}"] if result.avatar_warning else []
        if result.fsm_warning:
            warnings.append(f"Состояния диалогов бота не очищены: {escape(result.fsm_warning)}")
        notice = "✅ Мастер сброшен к эталону: данные заменены эталоном." + "".join(f"\n⚠️ {w}" for w in warnings)
        await _show_card(callback, session_factory, health_monitor, master_id, notice=notice)



    @router.callback_query(SaCallback.filter(F.action == "do_ow"))
    async def overwrite_reference(
        callback: CallbackQuery,
        callback_data: SaCallback,
        session_factory: async_sessionmaker,
        health_monitor: WebhookHealthMonitor | None = None,
    ) -> None:
        master_id = callback_data.master_id
        if master_id != reference.REFERENCE_MASTER_ID:  # кнопка есть только у мастера-эталона; чужие callback отклоняем
            await callback.answer("Сохранять эталон можно только у мастера-эталона.", show_alert=True)
            return
        card = await service.get_master_card(session_factory, master_id)
        bot_id = card.row.telegram_bot_id if card else None
        await callback.answer("Сохраняю эталон…")
        try:
            result = await reference.overwrite_reference(
                session_factory, TokenCipher.from_settings(), actor_telegram_id=callback.from_user.id
            )
        except (reference.ReferenceResetError, *MANAGEMENT_ERRORS) as exc:
            await service.log_action(
                session_factory, actor_telegram_id=callback.from_user.id, action="overwrite_reference", master_id=master_id,
                bot_id=bot_id, ok=False, detail=str(exc),
            )
            await _show(callback, f"❌ Эталон не сохранён, прежний остался без изменений: {escape(str(exc))}", _back_to_card(master_id))
            return
        await service.log_action(
            session_factory, actor_telegram_id=callback.from_user.id, action="overwrite_reference", master_id=master_id,
            bot_id=bot_id, detail=result.avatar_warning,
        )
        notice = (
            f"✅ Эталон сохранён: им стало текущее состояние мастера №{master_id} "
            f"(услуг: {result.services}, отзывов: {result.reviews}, фото портфолио: {result.portfolio}). "
            "Остальные мастера применят его кнопкой «Сбросить к эталону»."
            + (f"\n⚠️ {escape(result.avatar_warning)}" if result.avatar_warning else "")
        )
        await _show_card(callback, session_factory, health_monitor, master_id, notice=notice)

    return router
