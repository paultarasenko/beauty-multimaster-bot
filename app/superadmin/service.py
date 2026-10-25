"""Сервисные функции суперадмина: запросы ко ВСЕМ мастерам сразу и журнал действий.

Это единственное место суперадмин-слоя, где читаются таблицы мастеров целиком (без фильтра по одному master_id) —
явные функции вместо репозиториев мастера (см. белый список tests/static_guards.py). Персональных данных клиентов
здесь нет: только счётчики. Токенов и секретов тоже: наружу отдаются username бота, id и статусы.
Управление ботами (create_master/add_bot/remove_bot/refresh_webhook/check_bot) — в app/multibot/management.py.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import (
    Booking,
    BookingStatus,
    Master,
    MasterBot,
    MasterClient,
    Notification,
    NotificationStatus,
    SuperadminAction,
)
from app.database.models.superadmin_action import RESULT_ERROR, RESULT_OK
from app.multibot.redact import redact

logger = logging.getLogger(__name__)

MAX_DETAIL_LEN = 500
JOURNAL_LIMIT = 15
# Напоминание клиенту/мастеру, опоздавшее больше чем на столько, после повторного включения не отправляется (см. cancel_overdue_notifications).
OVERDUE_NOTIFICATION_GRACE = dt.timedelta(minutes=15)


@dataclass(frozen=True)
class Summary:
    masters_total: int
    masters_active: int
    bots_active: int
    bots_inactive: int
    clients_total: int  # связей «клиент—мастер» (человек у двух мастеров считается дважды)
    bookings_total: int
    bookings_confirmed: int

    @property
    def masters_inactive(self) -> int:
        return self.masters_total - self.masters_active


@dataclass(frozen=True)
class MasterRow:
    master_id: int
    owner_telegram_id: int | None
    is_active: bool
    telegram_bot_id: int | None
    bot_username: str | None
    bot_active: bool | None
    trial_ends_on: dt.date | None = None  # этап 10: последний день пробного периода включительно; None — без срока


@dataclass(frozen=True)
class DigestItem:
    """Мастер для ежедневной сводки о сроках (этап 10). Только числовые id и @username бота."""

    master_id: int
    ends_on: dt.date
    bot_username: str | None


@dataclass(frozen=True)
class MasterCard:
    row: MasterRow
    clients: int
    bookings: int
    bookings_confirmed: int
    created_at: dt.datetime
    bookings_upcoming: int = 0  # подтверждённые записи, которые ещё впереди: их сброс к эталону удалит без уведомления клиентов


@dataclass(frozen=True)
class ActionRecord:
    created_at: dt.datetime
    actor_telegram_id: int
    action: str
    target_master_id: int | None
    target_bot_id: int | None
    result: str
    detail: str | None


async def get_summary(session_factory: async_sessionmaker) -> Summary:
    async with session_factory() as session:
        masters_total = (await session.execute(select(func.count()).select_from(Master))).scalar_one()
        masters_active = (
            await session.execute(select(func.count()).select_from(Master).where(Master.is_active.is_(True)))
        ).scalar_one()
        bots_active = (
            await session.execute(select(func.count()).select_from(MasterBot).where(MasterBot.is_active.is_(True)))
        ).scalar_one()
        bots_total = (await session.execute(select(func.count()).select_from(MasterBot))).scalar_one()
        clients = (await session.execute(select(func.count()).select_from(MasterClient))).scalar_one()
        bookings = (await session.execute(select(func.count()).select_from(Booking))).scalar_one()
        confirmed = (
            await session.execute(
                select(func.count()).select_from(Booking).where(Booking.status == BookingStatus.CONFIRMED.value)
            )
        ).scalar_one()
    return Summary(
        masters_total=masters_total,
        masters_active=masters_active,
        bots_active=bots_active,
        bots_inactive=bots_total - bots_active,
        clients_total=clients,
        bookings_total=bookings,
        bookings_confirmed=confirmed,
    )


def _row(master: Master, bot: MasterBot | None) -> MasterRow:
    return MasterRow(
        master_id=master.id,
        owner_telegram_id=master.owner_telegram_id,
        is_active=master.is_active,
        telegram_bot_id=bot.telegram_bot_id if bot else None,
        bot_username=bot.bot_username if bot else None,
        bot_active=bot.is_active if bot else None,
        trial_ends_on=master.trial_ends_on,
    )


async def list_masters(session_factory: async_sessionmaker) -> list[MasterRow]:
    """Все мастера со статусом и ботом, по возрастанию id."""
    async with session_factory() as session:
        result = await session.execute(
            select(Master, MasterBot).outerjoin(MasterBot, MasterBot.master_id == Master.id).order_by(Master.id)
        )
        return [_row(master, bot) for master, bot in result.all()]


def match_masters(rows: list[MasterRow], query: str) -> list[MasterRow]:
    """Мастера, подходящие под запрос суперадмина. Число — точное совпадение с номером мастера, Telegram id владельца или
    id бота; иначе — часть username бота (регистр и ведущая «@» не важны). Пустой запрос ничего не находит."""
    needle = query.strip().lstrip("@").strip().casefold()
    if not needle:
        return []
    if needle.isdigit():
        number = int(needle)
        return [r for r in rows if number in (r.master_id, r.owner_telegram_id, r.telegram_bot_id)]
    return [r for r in rows if r.bot_username and needle in r.bot_username.casefold()]


async def get_master_card(session_factory: async_sessionmaker, master_id: int) -> MasterCard | None:
    async with session_factory() as session:
        result = await session.execute(
            select(Master, MasterBot).outerjoin(MasterBot, MasterBot.master_id == Master.id).where(Master.id == master_id)
        )
        found = result.first()
        if found is None:
            return None
        master, bot = found
        clients = (
            await session.execute(select(func.count()).select_from(MasterClient).where(MasterClient.master_id == master_id))
        ).scalar_one()
        bookings = (
            await session.execute(select(func.count()).select_from(Booking).where(Booking.master_id == master_id))
        ).scalar_one()
        confirmed = (
            await session.execute(
                select(func.count())
                .select_from(Booking)
                .where(Booking.master_id == master_id, Booking.status == BookingStatus.CONFIRMED.value)
            )
        ).scalar_one()
        upcoming = (
            await session.execute(
                select(func.count())
                .select_from(Booking)
                .where(
                    Booking.master_id == master_id,
                    Booking.status == BookingStatus.CONFIRMED.value,
                    Booking.start_datetime > func.now(),
                )
            )
        ).scalar_one()
        return MasterCard(
            row=_row(master, bot),
            clients=clients,
            bookings=bookings,
            bookings_confirmed=confirmed,
            created_at=master.created_at,
            bookings_upcoming=upcoming,
        )


async def cancel_overdue_notifications(session, master_id: int, now: dt.datetime | None = None) -> int:
    """Отменяет напоминания мастера, время которых прошло (pending с `scheduled_at` старше OVERDUE_NOTIFICATION_GRACE).

    Вызывается при ПОВТОРНОМ включении мастера/бота: пока он был отключён, планировщик его пропускал, и напоминания «копились».
    Без этого после включения клиентам ушла бы пачка устаревших сообщений («запись через 24 часа», когда до неё уже часы).
    Напоминания, время которых ещё не наступило, не трогаются и уходят в срок. Транзакцию коммитит вызывающий."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result = await session.execute(
        update(Notification)
        .where(
            Notification.master_id == master_id,
            Notification.status == NotificationStatus.PENDING.value,
            Notification.scheduled_at < now - OVERDUE_NOTIFICATION_GRACE,
        )
        .values(status=NotificationStatus.CANCELLED.value)
    )
    if result.rowcount:
        logger.info("Повторное включение: отменено устаревших напоминаний: %s (master_id=%s)", result.rowcount, master_id)
    return result.rowcount or 0


async def set_master_active(session_factory: async_sessionmaker, master_id: int, active: bool) -> bool:
    """Подключает/отключает мастера целиком (masters.is_active). Данные не удаляются. False — мастера нет.

    При включении устаревшие напоминания отменяются в той же транзакции (до того, как планировщик снова увидит мастера)."""
    async with session_factory() as session:
        if active:
            await cancel_overdue_notifications(session, master_id)
        result = await session.execute(update(Master).where(Master.id == master_id).values(is_active=active))
        await session.commit()
        return result.rowcount > 0


async def set_bot_active(session_factory: async_sessionmaker, master_id: int, active: bool) -> int | None:
    """Включает/выключает бота мастера (master_bots.is_active). Возвращает id бота в Telegram или None, если бота нет.

    Telegram не трогаем: webhook остаётся установленным, пока бот неактивен, апдейты принимаются и отбрасываются (ответ 200).
    При включении устаревшие напоминания отменяются так же, как при включении мастера."""
    async with session_factory() as session:
        bot_id = (
            await session.execute(select(MasterBot.telegram_bot_id).where(MasterBot.master_id == master_id))
        ).scalar_one_or_none()
        if bot_id is None:
            return None
        if active:
            await cancel_overdue_notifications(session, master_id)
        await session.execute(update(MasterBot).where(MasterBot.master_id == master_id).values(is_active=active))
        await session.commit()
        return bot_id


# ---- пробный период (этап 10) ----


async def set_trial_ends_on(session_factory: async_sessionmaker, master_id: int, ends_on: dt.date | None) -> bool:
    """Устанавливает срок пробного периода (None — без срока) и сбрасывает состояние уведомлений. False — мастера нет.

    Мастера не отключает и не включает: срок — только напоминание суперадмину."""
    async with session_factory() as session:
        result = await session.execute(
            update(Master)
            .where(Master.id == master_id)
            .values(trial_ends_on=ends_on, trial_expiring_notified_for=None, trial_expired_notified_on=None)
        )
        await session.commit()
        return result.rowcount > 0


async def set_owner_telegram_id(
    session_factory: async_sessionmaker, master_id: int, owner_telegram_id: int
) -> tuple[bool, int | None]:
    """Назначает мастеру владельца (Telegram id, которому открывается админ-панель его бота).

    Возвращает (найден ли мастер, прежний владелец). Права определяются masters.owner_telegram_id при каждом апдейте,
    поэтому смена действует сразу, без перезапуска."""
    async with session_factory() as session:
        row = (
            await session.execute(select(Master.owner_telegram_id).where(Master.id == master_id).with_for_update())
        ).first()
        if row is None:
            return False, None
        await session.execute(update(Master).where(Master.id == master_id).values(owner_telegram_id=owner_telegram_id))
        await session.commit()
        return True, row[0]


async def extend_trial(session_factory: async_sessionmaker, master_id: int, days: int, today: dt.date) -> dt.date | None:
    """Продлевает срок на `days` дней (от сегодня, если срока нет или он истёк, иначе от текущего окончания).

    Возвращает новый срок или None, если мастера нет. Чтение и запись — в одной транзакции под блокировкой строки."""
    from app.superadmin.trial import extended_ends_on

    async with session_factory() as session:
        found = (await session.execute(select(Master.trial_ends_on).where(Master.id == master_id).with_for_update())).first()
        if found is None:
            return None
        new_end = extended_ends_on(found[0], today, days)
        await session.execute(
            update(Master)
            .where(Master.id == master_id)
            .values(trial_ends_on=new_end, trial_expiring_notified_for=None, trial_expired_notified_on=None)
        )
        await session.commit()
        return new_end


async def list_digest_candidates(
    session_factory: async_sessionmaker, today: dt.date
) -> tuple[list[DigestItem], list[DigestItem]]:
    """Кому что сообщить в сегодняшней сводке: (заканчивается завтра и ещё не предупреждали; срок истёк и сегодня ещё не сообщали).

    Только подключённые (is_active) мастера со сроком; отключённые и «без срока» в сводку не попадают."""
    tomorrow = today + dt.timedelta(days=1)
    async with session_factory() as session:
        base = (
            select(Master.id, Master.trial_ends_on, MasterBot.bot_username)
            .outerjoin(MasterBot, MasterBot.master_id == Master.id)
            .where(Master.is_active.is_(True))
            .order_by(Master.trial_ends_on, Master.id)
        )
        expiring = (
            await session.execute(
                base.where(
                    Master.trial_ends_on == tomorrow,
                    Master.trial_expiring_notified_for.is_distinct_from(Master.trial_ends_on),
                )
            )
        ).all()
        expired = (
            await session.execute(
                base.where(
                    Master.trial_ends_on < today,
                    Master.trial_expired_notified_on.is_distinct_from(today),
                )
            )
        ).all()
    return [DigestItem(*row) for row in expiring], [DigestItem(*row) for row in expired]


async def mark_digest_sent(
    session_factory: async_sessionmaker, today: dt.date, expiring_ids: list[int], expired_ids: list[int]
) -> None:
    """Запоминает, что эти мастера вошли в отправленную сводку. Условия по сроку защищают от гонки: если суперадмин успел
    продлить срок между выборкой и отметкой, новое состояние не затирается."""
    tomorrow = today + dt.timedelta(days=1)
    async with session_factory() as session:
        if expiring_ids:
            await session.execute(
                update(Master)
                .where(Master.id.in_(expiring_ids), Master.trial_ends_on == tomorrow)
                .values(trial_expiring_notified_for=Master.trial_ends_on)
            )
        if expired_ids:
            await session.execute(
                update(Master)
                .where(Master.id.in_(expired_ids), Master.trial_ends_on < today)
                .values(trial_expired_notified_on=today)
            )
        await session.commit()


async def list_active_bot_targets(session_factory: async_sessionmaker) -> list[tuple[int, int]]:
    """(master_id, telegram_bot_id) всех активных ботов активных мастеров — для периодической проверки."""
    async with session_factory() as session:
        result = await session.execute(
            select(MasterBot.master_id, MasterBot.telegram_bot_id)
            .join(Master, Master.id == MasterBot.master_id)
            .where(MasterBot.is_active.is_(True), Master.is_active.is_(True))
            .order_by(MasterBot.master_id)
        )
        return [(master_id, bot_id) for master_id, bot_id in result.all()]


# ---- журнал действий ----


async def log_action(
    session_factory: async_sessionmaker,
    *,
    actor_telegram_id: int,
    action: str,
    master_id: int | None = None,
    bot_id: int | None = None,
    ok: bool = True,
    detail: str | None = None,
) -> None:
    """Запись в superadmin_actions. `detail` маскируется и обрезается; токены и секреты сюда передавать нельзя.

    Сбой записи журнала не должен ломать само действие, поэтому наружу не выбрасывается."""
    text = redact(detail)[:MAX_DETAIL_LEN] if detail else None
    try:
        async with session_factory() as session:
            session.add(
                SuperadminAction(
                    actor_telegram_id=actor_telegram_id,
                    action=action,
                    target_master_id=master_id,
                    target_bot_id=bot_id,
                    result=RESULT_OK if ok else RESULT_ERROR,
                    detail=text,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Не удалось записать действие суперадмина в журнал (action=%s)", action)


async def recent_actions(
    session_factory: async_sessionmaker, limit: int = JOURNAL_LIMIT, *, master_id: int | None = None
) -> list[ActionRecord]:
    """Последние действия суперадмина, новые сверху; `master_id` оставляет только действия над этим мастером."""
    query = select(SuperadminAction)
    if master_id is not None:
        query = query.where(SuperadminAction.target_master_id == master_id)
    async with session_factory() as session:
        rows = (
            await session.execute(query.order_by(SuperadminAction.created_at.desc(), SuperadminAction.id.desc()).limit(limit))
        ).scalars()
        return [
            ActionRecord(
                created_at=r.created_at,
                actor_telegram_id=r.actor_telegram_id,
                action=r.action,
                target_master_id=r.target_master_id,
                target_bot_id=r.target_bot_id,
                result=r.result,
                detail=r.detail,
            )
            for r in rows
        ]
