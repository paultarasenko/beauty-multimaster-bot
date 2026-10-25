"""Управление мастерами и их ботами (backend CLI `python -m app.cli` и суперадмин-бота, этап 9).

Telegram API вызывается только через `bot_factory` (в тестах подменяется), токен нигде не печатается и не логируется.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.fsm_storage import FsmStorageError, clear_bot_states_from_settings
from app.config.settings import settings
from app.database.models import Master, MasterBot
from app.multibot.constants import ALLOWED_UPDATES
from app.multibot.crypto import ConfigurationError, TokenCipher, generate_webhook_secret
from app.multibot.redact import redact
from app.multibot.telegram_api import telegram_session
from app.services.master_settings_service import ensure_master_defaults

logger = logging.getLogger(__name__)

BotFactory = Callable[[str], Bot]
# Очистка состояний FSM бота (этап 7): принимает id бота в Telegram, возвращает число удалённых записей
# (None — FSM в памяти процесса бота, из CLI не очистить). В тестах подменяется.
FsmCleaner = Callable[[int], Awaitable[int | None]]


class ManagementError(RuntimeError):
    """Ошибка команды управления; текст безопасен для вывода (токены замаскированы)."""


def default_bot_factory(token: str) -> Bot:
    return Bot(token=token, session=telegram_session())


async def default_fsm_cleaner(telegram_bot_id: int) -> int | None:
    return await clear_bot_states_from_settings(telegram_bot_id)


def normalize_public_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ConfigurationError("PUBLIC_BASE_URL не задан или не https (пример: https://bot.example.com)")
    return url


def webhook_url(public_base_url: str, telegram_bot_id: int, secret: str) -> str:
    return f"{normalize_public_base_url(public_base_url)}/tg/{telegram_bot_id}/{secret}"


@dataclass
class BotSummary:
    master_id: int
    owner_telegram_id: int | None
    master_active: bool
    telegram_bot_id: int | None = None
    bot_username: str | None = None
    bot_active: bool | None = None


async def create_master(
    session_factory: async_sessionmaker, owner_telegram_id: int, *, trial_ends_on: dt.date | None = None
) -> int:
    """Создаёт мастера. `trial_ends_on` — последний день пробного периода включительно (None — без срока, этап 10)."""
    async with session_factory() as session:
        master = Master(owner_telegram_id=owner_telegram_id, is_active=True, trial_ends_on=trial_ends_on)
        session.add(master)
        await session.flush()
        # Новый мастер работает из коробки: сразу создаются профиль и настройки по умолчанию (этап 8).
        await ensure_master_defaults(session, master.id)
        await session.commit()
        return master.id


async def _call_telegram(coro_factory, *secrets: str):
    """Вызов Telegram с маскированием токена в тексте ошибки."""
    try:
        return await coro_factory()
    except Exception as exc:
        raise ManagementError(f"Telegram: {type(exc).__name__}: {redact(str(exc), *secrets)}") from None


async def _set_webhook(bot: Bot, url: str, header_token: str, token: str) -> None:
    await _call_telegram(
        lambda: bot.set_webhook(url, secret_token=header_token, allowed_updates=ALLOWED_UPDATES),
        token,
        url,
        header_token,
    )


async def add_bot(
    session_factory: async_sessionmaker,
    cipher: TokenCipher,
    *,
    master_id: int,
    token: str,
    public_base_url: str,
    bot_factory: BotFactory = default_bot_factory,
    fsm_cleaner: FsmCleaner = default_fsm_cleaner,
) -> MasterBot:
    """Подключает бота мастеру: getMe → очистка старых состояний FSM бота → запись в БД (токен зашифрован) → setWebhook.
    При сбое setWebhook запись откатывается.

    Очистка нужна, когда тот же Telegram-бот раньше принадлежал другому мастеру: его незаконченные диалоги не должны
    достаться новому. Если очистить не удалось, бот НЕ подключается (иначе чужие состояния могли бы перейти новому мастеру)."""
    token = token.strip()
    normalize_public_base_url(public_base_url)  # fail fast

    async with session_factory() as session:
        if await session.get(Master, master_id) is None:
            raise ManagementError(f"Мастер id={master_id} не найден")
        if (await session.execute(select(MasterBot.id).where(MasterBot.master_id == master_id))).first():
            raise ManagementError(f"У мастера id={master_id} уже есть бот: сначала выполните remove-bot")

    bot = bot_factory(token)
    try:
        me = await _call_telegram(bot.get_me, token)
        secret = generate_webhook_secret()
        async with session_factory() as session:
            taken = (await session.execute(select(MasterBot.master_id).where(MasterBot.telegram_bot_id == me.id))).scalar()
            if taken is not None:
                raise ManagementError(f"Этот бот уже подключён к мастеру id={taken}")
            try:
                await fsm_cleaner(me.id)
            except FsmStorageError as exc:
                raise ManagementError(f"Не удалось очистить прежние состояния FSM бота, бот не подключён: {exc}") from None
            row = MasterBot(
                master_id=master_id,
                telegram_bot_id=me.id,
                bot_username=me.username,
                bot_token_encrypted=cipher.encrypt(token),
                webhook_secret=secret,
                is_active=True,
            )
            session.add(row)
            await session.commit()  # до setWebhook: первый же апдейт должен найти бота в БД
            session.expunge(row)
        try:
            await _set_webhook(bot, webhook_url(public_base_url, me.id, secret), cipher.header_token(secret), token)
        except ManagementError:
            async with session_factory() as session:
                stored = (await session.execute(select(MasterBot).where(MasterBot.telegram_bot_id == me.id))).scalar_one()
                await session.delete(stored)
                await session.commit()
            raise
        return row
    finally:
        await bot.session.close()


async def _get_bot_row(session, master_id: int) -> MasterBot:
    row = (await session.execute(select(MasterBot).where(MasterBot.master_id == master_id))).scalar_one_or_none()
    if row is None:
        raise ManagementError(f"У мастера id={master_id} нет подключённого бота")
    return row


async def remove_bot(
    session_factory: async_sessionmaker,
    cipher: TokenCipher,
    *,
    master_id: int,
    bot_factory: BotFactory = default_bot_factory,
    fsm_cleaner: FsmCleaner = default_fsm_cleaner,
) -> str | None:
    """Удаляет бота мастера: deleteWebhook у Telegram (если не вышло — предупреждение в результате), строка master_bots
    и состояния FSM этого бота (этап 7; если Redis недоступен — предупреждение: add_bot всё равно очистит их заново).

    Возвращает текст предупреждения или None. Данные мастера (записи, клиенты) не затрагиваются.
    """
    async with session_factory() as session:
        row = await _get_bot_row(session, master_id)
        token = cipher.decrypt(row.bot_token_encrypted)
        bot_id = row.telegram_bot_id

    warning = None
    bot = bot_factory(token)
    try:
        await _call_telegram(bot.delete_webhook, token)
    except ManagementError as exc:
        warning = f"webhook у Telegram снять не удалось ({exc}); бот удалён из БД, запросы к старому адресу получают 404"
    finally:
        await bot.session.close()

    async with session_factory() as session:
        row = await _get_bot_row(session, master_id)
        await session.delete(row)
        await session.commit()
    logger.info("Бот %s отключён от мастера %s", bot_id, master_id)

    fsm_warning = None
    try:
        cleared = await fsm_cleaner(bot_id)
    except FsmStorageError as exc:
        fsm_warning = f"состояния диалогов бота в Redis не очищены ({exc}); они будут очищены при повторном add-bot"
    else:
        if cleared is None:
            fsm_warning = (
                "FSM хранится в памяти процесса бота: состояния отключённого бота исчезнут при перезапуске приложения"
            )
        else:
            logger.info("Состояния FSM бота %s очищены (записей: %s)", bot_id, cleared)
    return "; ".join(w for w in (warning, fsm_warning) if w) or None


async def refresh_webhook(
    session_factory: async_sessionmaker,
    cipher: TokenCipher,
    *,
    master_id: int,
    public_base_url: str,
    rotate_secret: bool = False,
    bot_factory: BotFactory = default_bot_factory,
) -> MasterBot:
    """Повторно регистрирует webhook (например, после смены PUBLIC_BASE_URL); rotate_secret — выдаёт новый секрет."""
    normalize_public_base_url(public_base_url)
    previous_secret: str | None = None
    async with session_factory() as session:
        row = await _get_bot_row(session, master_id)
        token = cipher.decrypt(row.bot_token_encrypted)
        if rotate_secret:
            previous_secret = row.webhook_secret
            row.webhook_secret = generate_webhook_secret()
            await session.commit()
        session.expunge(row)

    bot = bot_factory(token)
    try:
        await _set_webhook(
            bot, webhook_url(public_base_url, row.telegram_bot_id, row.webhook_secret), cipher.header_token(row.webhook_secret), token
        )
    except ManagementError:
        if previous_secret is not None:
            # Telegram остался на старом адресе: возвращаем прежний секрет, иначе апдейты пошли бы на путь, которого бот не знает.
            async with session_factory() as session:
                await session.execute(
                    update(MasterBot).where(MasterBot.master_id == master_id).values(webhook_secret=previous_secret)
                )
                await session.commit()
        raise
    finally:
        await bot.session.close()
    return row


@dataclass
class BotCheck:
    """Результат проверки бота мастера (getMe + getWebhookInfo). Токена, секрета и адреса webhook здесь нет."""

    master_id: int
    telegram_bot_id: int
    is_active: bool  # master_bots.is_active
    bot_username: str | None = None
    bot_name: str | None = None
    token_valid: bool | None = None  # None — выяснить не удалось (нет связи с Telegram)
    webhook_registered: bool | None = None  # у Telegram установлен какой-нибудь webhook
    webhook_matches: bool | None = None  # адрес webhook совпадает с ожидаемым (None — PUBLIC_BASE_URL не задан)
    pending_updates: int | None = None
    last_error_message: str | None = None  # последняя ошибка доставки апдейтов (замаскирована)
    last_error_at: dt.datetime | None = None
    error: str | None = None  # проверка не состоялась: безопасный текст причины

    @property
    def webhook_ok(self) -> bool | None:
        """True — webhook установлен на наш адрес; False — нет; None — определить не удалось."""
        if self.webhook_registered is None:
            return None
        if not self.webhook_registered:
            return False
        return self.webhook_matches


async def check_bot(
    session_factory: async_sessionmaker,
    cipher: TokenCipher,
    *,
    master_id: int,
    public_base_url: str = "",
    bot_factory: BotFactory = default_bot_factory,
) -> BotCheck:
    """Проверяет бота мастера у Telegram: токен (getMe) и webhook (getWebhookInfo, адрес сверяется с ожидаемым).

    Ошибки Telegram не выбрасываются, а попадают в результат (токен замаскирован); `ManagementError` — только если у мастера нет бота.
    """
    async with session_factory() as session:
        row = await _get_bot_row(session, master_id)
        token = cipher.decrypt(row.bot_token_encrypted)
        secret = row.webhook_secret
        result = BotCheck(
            master_id=master_id, telegram_bot_id=row.telegram_bot_id, is_active=row.is_active, bot_username=row.bot_username
        )
    try:
        expected_url: str | None = webhook_url(public_base_url, result.telegram_bot_id, secret)
    except ConfigurationError:
        expected_url = None

    bot = bot_factory(token)
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            result.token_valid = False
            result.error = "Telegram отверг токен бота (Unauthorized)"
            return result
        except Exception as exc:
            result.error = f"Telegram: {type(exc).__name__}: {redact(str(exc), token, secret)}"
            return result
        result.token_valid = True
        result.bot_username, result.bot_name = me.username, me.first_name
        try:
            info = await bot.get_webhook_info()
        except Exception as exc:
            result.error = f"getWebhookInfo: {type(exc).__name__}: {redact(str(exc), token, secret)}"
            return result
        result.webhook_registered = bool(info.url)
        result.webhook_matches = None if expected_url is None else info.url == expected_url
        result.pending_updates = info.pending_update_count
        if info.last_error_message:
            result.last_error_message = redact(info.last_error_message, token, secret)[:300]
            result.last_error_at = info.last_error_date
        return result
    finally:
        await bot.session.close()


async def list_bots(session_factory: async_sessionmaker) -> list[BotSummary]:
    """Мастера и их боты. Токенов и секретов в результате нет."""
    async with session_factory() as session:
        result = await session.execute(
            select(Master, MasterBot).outerjoin(MasterBot, MasterBot.master_id == Master.id).order_by(Master.id)
        )
        return [
            BotSummary(
                master_id=m.id,
                owner_telegram_id=m.owner_telegram_id,
                master_active=m.is_active,
                telegram_bot_id=b.telegram_bot_id if b else None,
                bot_username=b.bot_username if b else None,
                bot_active=b.is_active if b else None,
            )
            for m, b in result.all()
        ]
