"""Эталонная база: сохранение данных мастера №1 как эталона и применение эталона к другим мастерам.

Две операции суперадмина (кнопки в карточке мастера):

* «Сохранить как эталон» (только у мастера №1) — `overwrite_reference`: текущее состояние мастера №1 копируется в БД
  (`reference_snapshots`) как новый эталон. Сам мастер №1 не меняется.
* «Сбросить к эталону» (у остальных мастеров) — `reset_master_to_reference`: данные мастера заменяются сохранённым эталоном.

В эталон входит всё, что мастер настраивает сам: профиль и его фото, настройки (интервал, валюта, лимиты, тексты сообщений),
услуги, отзывы, портфолио с фото, расписание (рабочие часы и обеденные перерывы) и фото бота (аватарка в Telegram). Записи,
клиенты, заблокированное время, напоминания и заявки в эталон не входят и при обновлении у мастера удаляются. Владелец, бот, токен, webhook и срок пробного
периода (`masters`, `master_bots`) не затрагиваются.

Фото в БД мастера хранятся как Telegram `file_id`, а он действует только в том боте, который загрузил файл. Поэтому в эталон
фото попадают самими файлами (скачиваются через бота мастера №1), а при обновлении бот мастера загружает их себе (отправка
суперадмину и сразу удаление сообщения). И то и другое — «всё или ничего»: если фото не скачались или не загрузились, база
не меняется.

Какие колонки копируются, определяют `_SPECS` и исключения в них: новая колонка таблицы мастера попадёт в эталон сама, а
колонка с привязкой к конкретному боту (как `file_id`) должна быть явно исключена и обработана как фото.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.fsm_storage import FsmStorageError
from app.database.models import (
    AdminReminder,
    BlockedPeriod,
    Booking,
    BookingLimitRequest,
    BreakPeriod,
    ClientAccessRequest,
    Master,
    MasterBot,
    MasterClient,
    MasterDeferredMessage,
    MasterProfile,
    MasterSettings,
    Notification,
    PortfolioItem,
    ReferenceSnapshot,
    ReferenceSnapshotPhoto,
    Review,
    Service,
    WorkingDayOverride,
    WorkingHours,
)
from app.multibot import management
from app.multibot.bot_avatar import BotPhotoError, remove_bot_avatar, set_bot_avatar
from app.multibot.crypto import TokenCipher
from app.multibot.redact import redact
from app.utils.master_settings import SETTING_FIELDS, default_value

logger = logging.getLogger(__name__)

# Мастер, с которого снимается эталон. Его бот скачивает фото эталона (только он может открыть их file_id).
REFERENCE_MASTER_ID = 1

PAYLOAD_VERSION = 1
AVATAR_SET, AVATAR_NONE, AVATAR_UNKNOWN = "set", "none", "unknown"  # состояние аватарки бота в эталоне
PROFILE_PHOTO_KEY = "profile"
AVATAR_KEY = "bot_avatar"

_NOT_COPIED = frozenset({"id", "master_id", "created_at", "updated_at"})
# Что копируется: модель и колонки, которые НЕ копируются. Отзывы сохраняют даты (от них зависит порядок: новые выше), но
# не привязку к клиенту; file_id заменяются файлами.
_SPECS: dict[str, tuple[type, frozenset[str]]] = {
    "profile": (MasterProfile, _NOT_COPIED | {"photo_file_id"}),
    "settings": (MasterSettings, _NOT_COPIED),
    "service": (Service, _NOT_COPIED),
    "review": (Review, frozenset({"id", "master_id", "client_id"})),
    "portfolio": (PortfolioItem, _NOT_COPIED | {"telegram_file_id"}),
    "working_hours": (WorkingHours, _NOT_COPIED),
    "break": (BreakPeriod, _NOT_COPIED),
}

# Порядок важен: сначала зависимые таблицы (уведомления → записи → услуги).
_CLEARED_MODELS = (
    Notification,
    Booking,
    BookingLimitRequest,
    ClientAccessRequest,
    AdminReminder,
    BlockedPeriod,
    BreakPeriod,
    Review,
    PortfolioItem,
    Service,
    WorkingDayOverride,
    WorkingHours,
    MasterClient,
    MasterProfile,
    MasterSettings,
    # Сообщения, отложенные тихим режимом, относятся к стёртым записям и запросам клиентов — устарели. Сам тихий режим
    # (master_quiet_mode) — личная настройка мастера, не часть эталона: сброс его не трогает.
    MasterDeferredMessage,
)

_REPEATABLE_READ = {"isolation_level": "REPEATABLE READ"}


class ReferenceResetError(RuntimeError):
    """Операция не выполнена; текст безопасен для показа суперадмину (токены замаскированы), база при этом не менялась."""


@dataclass
class ResetResult:
    fsm_warning: str | None = None
    avatar_warning: str | None = None


@dataclass
class OverwriteResult:
    services: int
    reviews: int
    portfolio: int
    profile_photo: bool
    avatar: str  # AVATAR_SET / AVATAR_NONE / AVATAR_UNKNOWN
    avatar_warning: str | None = None


@dataclass(frozen=True)
class LoadedReference:
    payload: dict[str, Any]
    photos: dict[str, bytes]


# ---- значения строк <-> JSON ----


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.time)):
        return value.isoformat()
    return value


def _decode(column, value: Any) -> Any:
    if value is None:
        return None
    python_type = column.type.python_type
    if python_type is Decimal:
        return Decimal(value)
    if python_type is dt.datetime:
        return dt.datetime.fromisoformat(value)
    if python_type is dt.time:
        return dt.time.fromisoformat(value)
    return value


def _dump(spec: str, row) -> dict[str, Any]:
    model, skip = _SPECS[spec]
    return {c.key: _encode(getattr(row, c.key)) for c in model.__table__.columns if c.key not in skip}


def _restore(spec: str, values: dict[str, Any]) -> dict[str, Any]:
    model, skip = _SPECS[spec]
    return {c.key: _decode(c, values[c.key]) for c in model.__table__.columns if c.key not in skip}


# ---- доступ к ботам ----


async def _bot_token(session_factory: async_sessionmaker, cipher: TokenCipher, master_id: int) -> tuple[MasterBot, str] | None:
    async with session_factory() as session:
        bot_row = (await session.execute(select(MasterBot).where(MasterBot.master_id == master_id))).scalar_one_or_none()
    return None if bot_row is None else (bot_row, cipher.decrypt(bot_row.bot_token_encrypted))


async def _download(bot: Bot, file_id: str, *, master_id: int, token: str) -> bytes:
    """Скачивает файл через бота мастера (только он может открыть свои file_id)."""
    try:
        file = await bot.get_file(file_id)
        if not file.file_path:
            raise ReferenceResetError("Telegram не вернул путь к фото мастера.")
        content = await bot.download_file(file.file_path)
    except TelegramAPIError as exc:
        raise ReferenceResetError(
            f"Не удалось скачать фото через бота мастера №{master_id}: {type(exc).__name__}: {redact(str(exc), token)}. "
            "Фото должен открывать бот, который его загружал."
        ) from None
    if content is None:
        raise ReferenceResetError("Telegram не отдал фото мастера.")
    return content.read()


async def _download_avatar(bot: Bot, telegram_bot_id: int, *, master_id: int, token: str) -> tuple[bytes | None, str | None]:
    """Аватарка бота: (файл или None, если её нет). Если прочитать не удалось — (None, текст причины)."""
    try:
        photos = await bot.get_user_profile_photos(user_id=telegram_bot_id, limit=1)
        if not photos.photos:
            return None, None
        return await _download(bot, photos.photos[0][-1].file_id, master_id=master_id, token=token), None
    except TelegramAPIError as exc:
        return None, f"{type(exc).__name__}: {redact(str(exc), token)}"
    except ReferenceResetError as exc:
        return None, str(exc)


async def _delete_quietly(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            logger.warning("Не удалось удалить служебное сообщение с фото эталона (message_id=%s)", message_id)


async def _upload_photos(
    bot: Bot, chat_id: int, photos: dict[str, bytes], *, bot_username: str | None, token: str
) -> dict[str, str]:
    """Загружает фото через бота мастера и возвращает их file_id по ключам `photos`."""
    file_ids: dict[str, str] = {}
    message_ids: list[int] = []
    try:
        for key, content in photos.items():
            message = await bot.send_photo(
                chat_id, BufferedInputFile(content, filename=f"{key.replace(':', '_')}.jpg"), disable_notification=True
            )
            message_ids.append(message.message_id)
            if not message.photo:
                raise ReferenceResetError("Telegram не вернул фото в ответ на загрузку. Повторите позже.")
            file_ids[key] = message.photo[-1].file_id
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        await _delete_quietly(bot, chat_id, message_ids)
        name = f"@{bot_username}" if bot_username else "бот мастера"
        raise ReferenceResetError(
            f"{name} не может отправить вам файл ({redact(str(exc), token)}). "
            "Откройте этого бота и нажмите /start со своего аккаунта, затем повторите."
        ) from None
    except TelegramAPIError as exc:
        await _delete_quietly(bot, chat_id, message_ids)
        raise ReferenceResetError(f"Telegram: {type(exc).__name__}: {redact(str(exc), token)}") from None
    except ReferenceResetError:
        await _delete_quietly(bot, chat_id, message_ids)
        raise
    await _delete_quietly(bot, chat_id, message_ids)
    return file_ids


# ---- «Сохранить как эталон»: снимок мастера №1 ----


async def overwrite_reference(
    session_factory: async_sessionmaker,
    cipher: TokenCipher,
    *,
    actor_telegram_id: int | None = None,
    bot_factory: management.BotFactory | None = None,
    source_master_id: int = REFERENCE_MASTER_ID,
) -> OverwriteResult:
    """Сохраняет текущее состояние мастера-эталона как новый эталон. Данные самого мастера не меняются.

    Всё или ничего: данные читаются одним согласованным снимком БД, все фото скачиваются до записи, а новый эталон
    заменяет прежний одной транзакцией. Единственное исключение — аватарка бота: если Telegram не дал её прочитать,
    эталон сохраняется без неё (при обновлении аватарки других ботов не трогаются), а причина возвращается в результате."""
    bot_factory = bot_factory or management.default_bot_factory
    source = await _bot_token(session_factory, cipher, source_master_id)
    if source is None:
        raise ReferenceResetError(
            f"У мастера №{source_master_id} нет бота: фото эталона скачиваются через его бота. Сначала подключите бота."
        )
    bot_row, token = source

    async with session_factory() as session:
        await session.connection(execution_options=_REPEATABLE_READ)  # профиль, услуги и т.д. — из одного момента времени
        profile = (await session.execute(select(MasterProfile).where(MasterProfile.master_id == source_master_id))).scalar_one_or_none()
        if profile is None:
            raise ReferenceResetError(f"У мастера №{source_master_id} нет профиля, снимать с него эталон нечего.")
        settings_row = (await session.execute(select(MasterSettings).where(MasterSettings.master_id == source_master_id))).scalar_one_or_none()
        services = (await session.execute(select(Service).where(Service.master_id == source_master_id).order_by(Service.sort_order, Service.id))).scalars().all()
        reviews = (await session.execute(select(Review).where(Review.master_id == source_master_id).order_by(Review.created_at, Review.id))).scalars().all()
        items = (await session.execute(select(PortfolioItem).where(PortfolioItem.master_id == source_master_id).order_by(PortfolioItem.sort_order, PortfolioItem.id))).scalars().all()
        hours = (await session.execute(select(WorkingHours).where(WorkingHours.master_id == source_master_id).order_by(WorkingHours.weekday))).scalars().all()
        breaks = (await session.execute(select(BreakPeriod).where(BreakPeriod.master_id == source_master_id).order_by(BreakPeriod.weekday, BreakPeriod.start_time, BreakPeriod.id))).scalars().all()

        payload: dict[str, Any] = {
            "version": PAYLOAD_VERSION,
            "profile": _dump("profile", profile),
            "profile_photo": bool(profile.photo_file_id),
            "settings": _dump("settings", settings_row) if settings_row is not None else {name: default_value(name) for name in SETTING_FIELDS},
            "services": [_dump("service", s) for s in services],
            "reviews": [_dump("review", r) for r in reviews],
            "portfolio": [{**_dump("portfolio", item), "photo": f"portfolio:{number}"} for number, item in enumerate(items)],
            "working_hours": [_dump("working_hours", h) for h in hours],
            "breaks": [_dump("break", b) for b in breaks],
        }
        file_ids = {f"portfolio:{number}": item.telegram_file_id for number, item in enumerate(items)}
        if profile.photo_file_id:
            file_ids[PROFILE_PHOTO_KEY] = profile.photo_file_id

    photos: dict[str, bytes] = {}
    avatar_warning: str | None = None
    bot = bot_factory(token)
    try:
        for key, file_id in file_ids.items():
            photos[key] = await _download(bot, file_id, master_id=source_master_id, token=token)
        avatar, avatar_error = await _download_avatar(bot, bot_row.telegram_bot_id, master_id=source_master_id, token=token)
    finally:
        await bot.session.close()
    if avatar_error is not None:
        payload["bot_avatar"] = AVATAR_UNKNOWN
        avatar_warning = f"Фото бота не скопировано, у других мастеров оно не изменится: {avatar_error}"
    elif avatar is None:
        payload["bot_avatar"] = AVATAR_NONE
    else:
        payload["bot_avatar"] = AVATAR_SET
        photos[AVATAR_KEY] = avatar

    async with session_factory() as session:
        snapshot = ReferenceSnapshot(source_master_id=source_master_id, payload=payload, created_by_telegram_id=actor_telegram_id)
        session.add(snapshot)
        await session.flush()
        session.add_all(ReferenceSnapshotPhoto(snapshot_id=snapshot.id, key=key, content=content) for key, content in photos.items())
        await session.execute(delete(ReferenceSnapshot).where(ReferenceSnapshot.id < snapshot.id))  # фото уйдут каскадом
        await session.commit()

    return OverwriteResult(
        services=len(services),
        reviews=len(reviews),
        portfolio=len(items),
        profile_photo=bool(profile.photo_file_id),
        avatar=payload["bot_avatar"],
        avatar_warning=avatar_warning,
    )


async def reference_saved_at(session_factory: async_sessionmaker) -> dt.datetime | None:
    """Когда сохранён действующий эталон (None — эталон ещё не сохранялся)."""
    async with session_factory() as session:
        return (
            await session.execute(select(ReferenceSnapshot.created_at).order_by(ReferenceSnapshot.id.desc()).limit(1))
        ).scalar_one_or_none()


# ---- «Сбросить к эталону»: применение эталона ----


async def _load_reference(session_factory: async_sessionmaker) -> LoadedReference | None:
    async with session_factory() as session:
        await session.connection(execution_options=_REPEATABLE_READ)  # заголовок и фото — одной версии, даже при перезаписи
        snapshot = (await session.execute(select(ReferenceSnapshot).order_by(ReferenceSnapshot.id.desc()).limit(1))).scalar_one_or_none()
        if snapshot is None:
            return None
        rows = (await session.execute(select(ReferenceSnapshotPhoto).where(ReferenceSnapshotPhoto.snapshot_id == snapshot.id))).scalars().all()
        return LoadedReference(payload=snapshot.payload, photos={row.key: row.content for row in rows})


def _required_photo_keys(payload: dict[str, Any]) -> list[str]:
    keys = [item["photo"] for item in payload["portfolio"]]
    if payload["profile_photo"]:
        keys.append(PROFILE_PHOTO_KEY)
    return keys


def _fill(session, master_id: int, payload: dict[str, Any], file_ids: dict[str, str]) -> None:
    session.add(MasterProfile(master_id=master_id, photo_file_id=file_ids.get(PROFILE_PHOTO_KEY), **_restore("profile", payload["profile"])))
    session.add(MasterSettings(master_id=master_id, **_restore("settings", payload["settings"])))
    for values in payload["services"]:  # порядок вставки = порядок id: выбор услуг сортируется по (sort_order, id)
        session.add(Service(master_id=master_id, **_restore("service", values)))
    for values in payload["reviews"]:
        session.add(Review(master_id=master_id, client_id=None, **_restore("review", values)))
    for values in payload["portfolio"]:
        session.add(PortfolioItem(master_id=master_id, telegram_file_id=file_ids[values["photo"]], **_restore("portfolio", values)))
    for values in payload["working_hours"]:
        session.add(WorkingHours(master_id=master_id, **_restore("working_hours", values)))
    for values in payload.get("breaks", []):  # эталон, снятый до появления обедов в снимке, перерывов не содержит
        session.add(BreakPeriod(master_id=master_id, **_restore("break", values)))


async def reset_master_to_reference(
    session_factory: async_sessionmaker,
    cipher: TokenCipher,
    *,
    master_id: int,
    upload_chat_id: int,
    bot_factory: management.BotFactory | None = None,
    fsm_cleaner: management.FsmCleaner | None = None,
    source_master_id: int = REFERENCE_MASTER_ID,
) -> ResetResult:
    """Заменяет данные мастера сохранённым эталоном. `upload_chat_id` — чат, в который бот мастера временно отправляет фото
    (аккаунт суперадмина: он должен хотя бы раз нажать /start в этом боте).

    Всё или ничего: сначала фото загружаются через бота мастера, и только потом в одной транзакции удаляются старые данные
    и создаются эталонные. Аватарка бота меняется после записи в БД и откатить её нельзя: при сбое вернётся предупреждение."""
    bot_factory = bot_factory or management.default_bot_factory
    fsm_cleaner = fsm_cleaner or management.default_fsm_cleaner
    if master_id == source_master_id:
        raise ReferenceResetError(f"Мастер №{source_master_id} — источник эталона: его не сбрасывают к эталону, а сохраняют как эталон.")

    target = await _bot_token(session_factory, cipher, master_id)
    if target is None:
        raise ReferenceResetError("У мастера нет бота: фото эталона загружаются через бота мастера. Сначала подключите бота.")
    bot_row, token = target

    reference = await _load_reference(session_factory)
    if reference is None:
        raise ReferenceResetError(
            f"Эталон ещё не создан. Откройте карточку мастера №{source_master_id} и нажмите «Сохранить как эталон»."
        )
    payload, stored = reference.payload, reference.photos
    required = _required_photo_keys(payload)
    if any(key not in stored for key in required):
        raise ReferenceResetError("Сохранённый эталон повреждён (нет файлов фото). Сохраните эталон заново у мастера №1.")

    bot = bot_factory(token)
    try:
        file_ids = await _upload_photos(bot, upload_chat_id, {key: stored[key] for key in required}, bot_username=bot_row.bot_username, token=token)
    finally:
        await bot.session.close()

    async with session_factory() as session:
        locked = (await session.execute(select(Master.id).where(Master.id == master_id).with_for_update())).first()
        if locked is None:
            raise ReferenceResetError("Мастер не найден (возможно, удалён).")
        for model in _CLEARED_MODELS:
            await session.execute(delete(model).where(model.master_id == master_id))
        _fill(session, master_id, payload, file_ids)
        await session.commit()

    result = ResetResult()
    result.avatar_warning = await _apply_avatar(bot_factory, token, payload.get("bot_avatar"), stored.get(AVATAR_KEY))
    try:
        await fsm_cleaner(bot_row.telegram_bot_id)  # незаконченные диалоги прежнего тестера ссылаются на удалённые данные
    except FsmStorageError as exc:
        result.fsm_warning = str(exc)
    return result


async def _apply_avatar(bot_factory: management.BotFactory, token: str, state: str | None, content: bytes | None) -> str | None:
    """Ставит или снимает аватарку бота по эталону. Возвращает текст предупреждения при сбое (данные мастера уже заменены)."""
    if state not in (AVATAR_SET, AVATAR_NONE):  # неизвестно — аватарку не трогаем
        return None
    if state == AVATAR_SET and content is None:
        return "Фото бота не изменено: в эталоне нет файла аватарки. Сохраните эталон заново у мастера №1."
    bot = bot_factory(token)
    try:
        if state == AVATAR_SET:
            await set_bot_avatar(bot, content)
        else:
            await remove_bot_avatar(bot)
    except BotPhotoError as exc:
        return f"Фото бота не изменено: {redact(str(exc), token)}"
    finally:
        await bot.session.close()
    return None
