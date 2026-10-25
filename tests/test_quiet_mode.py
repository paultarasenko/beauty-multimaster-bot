"""Тихий режим мастера: окно времени, откладывание уведомлений, доставка после окончания, экран в настройках.

БД настоящая, Telegram замокан (`tests/tg.py`). Часовой пояс мастера в тестах — «Etc/GMT-3» (UTC+3 без перехода на летнее время),
чтобы результат не зависел от даты прогона.
"""

import datetime as dt

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.bot.handlers.admin import master_settings as admin_settings
from app.bot.keyboards.callback_data import AdminSettingsCallback
from app.bot.states.booking_states import AdminSettingsFSM
from app.database.models import (
    AdminReminder,
    Booking,
    Master,
    MasterDeferredMessage,
    MasterProfile,
    MasterQuietMode,
    Notification,
    Service,
    User,
)
from app.database.repositories.quiet_mode_repository import DeferredMessageRepository, QuietModeRepository
from app.scheduler.reminder_worker import make_send_deferred_func
from app.services.delivery import DeliveryStop, Outcome
from app.services.notifier import MasterNotifier
from app.services.quiet_mode_service import defer_if_quiet, deliver_deferred, is_quiet_now
from app.utils.quiet_mode import QuietModeError, is_quiet_time, validate_window
from tests.conftest import MASTER_ID, make_client, set_owner
from tests.test_stage6_scheduler import BOT, OWNER, UTC, world  # noqa: F401  (фикстура world)
from tests.tg import BOT_ID, RecordingSession, tg_harness

ADMIN = 999
TZ = dt.timezone(dt.timedelta(hours=3))
TZ_NAME = "Etc/GMT-3"
T = dt.time


def utc(hour: int, minute: int = 0, day: int = 23) -> dt.datetime:
    """Момент в UTC; по часам мастера (UTC+3) — на три часа позже."""
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=UTC)


async def setup_master(session, *, enabled=True, start=T(22), end=T(8), owner: int | None = ADMIN) -> None:
    session.add(MasterProfile(master_id=MASTER_ID, name="Мастер", timezone=TZ_NAME))
    await session.execute(update(Master).where(Master.id == MASTER_ID).values(owner_telegram_id=owner))
    await QuietModeRepository(session, MASTER_ID).save(enabled=enabled, start_time=start, end_time=end)
    await session.commit()


async def deferred(session, master_id: int = MASTER_ID) -> list[MasterDeferredMessage]:
    session.expire_all()
    result = await session.execute(
        select(MasterDeferredMessage).where(MasterDeferredMessage.master_id == master_id).order_by(MasterDeferredMessage.id)
    )
    return list(result.scalars().all())


def around_now(*, inside: bool) -> tuple[dt.time, dt.time]:
    """Окно (по часам мастера), которое сейчас действует (inside=True) или точно не действует."""
    now_local = dt.datetime.now(TZ)
    shift = dt.timedelta(hours=-1) if inside else dt.timedelta(hours=2)
    start = (now_local + shift).time()
    end = (now_local + shift + dt.timedelta(hours=2 if inside else 1)).time()
    return start, end


# ---------------------------------------------------------------- чистые функции


@pytest.mark.parametrize(
    ("start", "end", "now", "expected"),
    [
        (T(22), T(8), T(23, 30), True),
        (T(22), T(8), T(3), True),
        (T(22), T(8), T(8), False),  # конец не включается
        (T(22), T(8), T(22), True),  # начало включается
        (T(22), T(8), T(12), False),
        (T(13), T(15), T(14), True),  # окно внутри суток
        (T(13), T(15), T(15), False),
        (T(13), T(15), T(12, 59), False),
        (T(9), T(9), T(9), False),  # пустое окно
    ],
)
def test_is_quiet_time(start, end, now, expected):
    assert is_quiet_time(start, end, now) is expected


def test_validate_window_rejects_equal_start_and_end():
    validate_window(T(22), T(8))
    validate_window(T(13), T(15))
    with pytest.raises(QuietModeError, match="не должны совпадать"):
        validate_window(T(9), T(9))


# ---------------------------------------------------------------- БД и сервис


async def test_window_cannot_be_empty_in_db(session):
    session.add(MasterQuietMode(master_id=MASTER_ID, enabled=True, start_time=T(9), end_time=T(9)))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_no_row_means_quiet_mode_is_off(session, master_profile):
    assert await is_quiet_now(session, MASTER_ID, utc(21)) is False
    assert (await QuietModeRepository(session, MASTER_ID).get_data()).enabled is False


async def test_is_quiet_now_uses_master_timezone_and_enabled_flag(session):
    await setup_master(session)
    assert await is_quiet_now(session, MASTER_ID, utc(20, 30)) is True  # 23:30 у мастера
    assert await is_quiet_now(session, MASTER_ID, utc(1, 0)) is True  # 04:00
    assert await is_quiet_now(session, MASTER_ID, utc(5, 0)) is False  # 08:00 — окно закончилось
    assert await is_quiet_now(session, MASTER_ID, utc(10, 0)) is False
    await QuietModeRepository(session, MASTER_ID).save(enabled=False, start_time=T(22), end_time=T(8))
    await session.commit()
    assert await is_quiet_now(session, MASTER_ID, utc(20, 30)) is False


async def test_defer_if_quiet_stores_text_markup_and_expiry(session):
    await setup_master(session)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Да", callback_data="x:1")]])
    expires = utc(23)
    assert await defer_if_quiet(session, MASTER_ID, "привет", markup, expires_at=expires, now=utc(20)) is True
    assert await defer_if_quiet(session, MASTER_ID, "день", now=utc(10)) is False
    await session.commit()
    [row] = await deferred(session)
    assert row.text == "привет" and row.expires_at == expires
    assert InlineKeyboardMarkup.model_validate(row.reply_markup) == markup


# ---------------------------------------------------------------- MasterNotifier


def notifier_for(session, *, with_session: bool = True):
    tg = RecordingSession()
    bot = Bot(token=f"{BOT_ID}:TEST", session=tg)
    return MasterNotifier(bot, master_id=MASTER_ID, owner_telegram_id=ADMIN, session=session if with_session else None), tg


async def test_new_user_notice_is_deferred_in_quiet_time(session):
    start, end = around_now(inside=True)
    await setup_master(session, start=start, end=end)
    n, tg = notifier_for(session)
    await n.notify_new_user(User(telegram_id=5, first_name="Анна"))
    assert tg.sent_to(ADMIN) == []
    [row] = await deferred(session)
    assert "Новый пользователь" in row.text and "Анна" in row.text


async def test_notice_goes_immediately_outside_quiet_time(session):
    start, end = around_now(inside=False)
    await setup_master(session, start=start, end=end)
    n, tg = notifier_for(session)
    await n.notify_new_user(User(telegram_id=5, first_name="Анна"))
    assert len(tg.sent_to(ADMIN)) == 1 and await deferred(session) == []


async def test_notifier_without_session_never_defers(session):
    start, end = around_now(inside=True)
    await setup_master(session, start=start, end=end)
    n, tg = notifier_for(session, with_session=False)
    await n.notify_new_user(User(telegram_id=5, first_name="Анна"))
    assert len(tg.sent_to(ADMIN)) == 1 and await deferred(session) == []


async def test_answer_to_masters_own_action_is_not_deferred(session):
    start, end = around_now(inside=True)
    await setup_master(session, start=start, end=end)
    n, tg = notifier_for(session)
    client = User(telegram_id=5, first_name="Анна")
    await n.notify_master_client_restricted(client, [], TZ)
    assert len(tg.sent_to(ADMIN)) == 1 and await deferred(session) == []


async def test_client_request_is_deferred_with_its_buttons_and_counts_as_delivered(session):
    from app.database.models import BookingLimitRequest

    start, end = around_now(inside=True)
    await setup_master(session, start=start, end=end)
    client = await make_client(session, 777, first_name="Анна")
    request = BookingLimitRequest(master_id=MASTER_ID, user_id=client.id, message="Можно ещё одну?")
    session.add(request)
    await session.flush()
    n, tg = notifier_for(session)
    assert await n.notify_limit_request(request, client) is True
    assert tg.sent_to(ADMIN) == []
    [row] = await deferred(session)
    assert "Можно ещё одну?" in row.text
    buttons = InlineKeyboardMarkup.model_validate(row.reply_markup).inline_keyboard
    assert buttons and all(b.callback_data for r in buttons for b in r)


async def test_deferral_failure_falls_back_to_immediate_send(session, monkeypatch):
    start, end = around_now(inside=True)
    await setup_master(session, start=start, end=end)

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.notifier.defer_if_quiet", broken)
    n, tg = notifier_for(session)
    await n.notify_new_user(User(telegram_id=5, first_name="Анна"))
    assert len(tg.sent_to(ADMIN)) == 1  # лучше потревожить мастера, чем потерять уведомление


# ---------------------------------------------------------------- доставка после окончания


def sender(tg: RecordingSession):
    return make_send_deferred_func(Bot(token=f"{BOT_ID}:TEST", session=tg), ADMIN)


async def test_deferred_messages_arrive_in_order_with_event_time_after_quiet_ends(session):
    await setup_master(session)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Принять", callback_data="x:1")]])
    await defer_if_quiet(session, MASTER_ID, "первое", markup, now=utc(20, 30))
    await defer_if_quiet(session, MASTER_ID, "второе", now=utc(21, 0))
    await session.commit()
    # время события — по часам БД (created_at = now()), поэтому подменяем его явно
    for row, created in zip(await deferred(session), (utc(20, 30), utc(21, 0)), strict=True):
        row.created_at = created
    await session.commit()
    tg = RecordingSession()

    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(1, 0, day=24)) == 0  # 04:00 — ещё тихо
    assert tg.sent_to(ADMIN) == [] and len(await deferred(session)) == 2

    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(5, 5, day=24)) == 2  # 08:05
    assert tg.sent_to(ADMIN) == ["🌙 Тихий режим · 23.10 23:30\n\nпервое", "🌙 Тихий режим · 00:00\n\nвторое"]
    first = next(r for r in tg.requests if r.text.endswith("первое"))
    assert first.reply_markup.inline_keyboard[0][0].callback_data == "x:1"
    assert await deferred(session) == []


async def test_event_time_has_no_date_when_delivered_same_day(session):
    await setup_master(session, start=T(13), end=T(15))
    await defer_if_quiet(session, MASTER_ID, "сообщение", now=utc(10, 30))  # 13:30 у мастера
    await session.commit()
    [row] = await deferred(session)
    row.created_at = utc(10, 30)
    await session.commit()
    tg = RecordingSession()
    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(12, 5)) == 1  # 15:05, тот же день
    assert tg.sent_to(ADMIN) == ["🌙 Тихий режим · 13:30\n\nсообщение"]


async def test_disabling_quiet_mode_releases_backlog(session):
    await setup_master(session)
    await defer_if_quiet(session, MASTER_ID, "накопилось", now=utc(20, 30))
    await session.commit()
    tg = RecordingSession()
    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(21, 0)) == 0
    await QuietModeRepository(session, MASTER_ID).save(enabled=False, start_time=T(22), end_time=T(8))
    await session.commit()
    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(21, 0)) == 1


async def test_expired_message_is_dropped_not_sent(session):
    await setup_master(session)
    await defer_if_quiet(session, MASTER_ID, "через 2 часа", expires_at=utc(5, 0), now=utc(20, 30))  # запись в 08:00
    await defer_if_quiet(session, MASTER_ID, "актуальное", now=utc(20, 31))
    await session.commit()
    tg = RecordingSession()
    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(5, 30)) == 1
    assert [t.split("\n\n")[1] for t in tg.sent_to(ADMIN)] == ["актуальное"]
    assert await deferred(session) == []


async def test_undeliverable_message_is_dropped_not_retried_forever(session):
    await setup_master(session)
    await defer_if_quiet(session, MASTER_ID, "мастер заблокировал бота", now=utc(20, 30))
    await session.commit()
    tg = RecordingSession()
    tg.blocked_chat_ids.add(ADMIN)
    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(6, 0)) == 0
    assert await deferred(session) == []


async def test_temporary_failure_keeps_messages_and_stops_the_bot(session):
    await setup_master(session)
    await defer_if_quiet(session, MASTER_ID, "первое", now=utc(20, 30))
    await defer_if_quiet(session, MASTER_ID, "второе", now=utc(20, 31))
    await session.commit()

    async def failing(text, markup):
        raise TelegramNetworkError(method=None, message="connection reset")

    with pytest.raises(DeliveryStop) as stop:
        await deliver_deferred(session, MASTER_ID, failing, now=utc(6, 0))
    assert stop.value.outcome is Outcome.STOP_TRANSIENT
    assert len(await deferred(session)) == 2  # ничего не потеряно
    tg = RecordingSession()
    assert await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(6, 1)) == 2


async def test_deferred_messages_of_other_master_are_untouched(session):
    await setup_master(session)
    session.add(Master(id=2, owner_telegram_id=888, is_active=True))
    await session.commit()
    await DeferredMessageRepository(session, 2).add("чужое")
    await session.commit()
    tg = RecordingSession()
    await deliver_deferred(session, MASTER_ID, sender(tg), now=utc(6, 0))
    assert [m.text for m in await deferred(session, 2)] == ["чужое"] and tg.sent_to(ADMIN) == []


# ---------------------------------------------------------------- планировщик


def due_admin_reminder(master_id: int, text: str = "позвонить поставщику") -> AdminReminder:
    return AdminReminder(
        master_id=master_id, text=text, recurrence="once", anchor_date=dt.date(2026, 1, 1),
        remind_time=T(9), next_fire_at=dt.datetime.now(UTC) - dt.timedelta(minutes=1),
    )


async def test_admin_reminder_in_quiet_time_is_deferred_then_delivered(world):  # noqa: F811
    await world.add_master(1, tz=TZ_NAME)
    start, end = around_now(inside=True)
    await QuietModeRepository(world.session, 1).save(enabled=True, start_time=start, end_time=end)
    world.session.add(due_admin_reminder(1))
    await world.session.commit()

    await world.scheduler().tick_admin_reminders()
    assert world.tg.chats_of(BOT(1)) == []  # во время тихого режима ничего не пришло
    [row] = await deferred(world.session, 1)
    assert "позвонить поставщику" in row.text
    assert (await world.session.execute(select(AdminReminder.id))).all() == []  # разовое напоминание «сработало»

    await QuietModeRepository(world.session, 1).save(enabled=False, start_time=start, end_time=end)
    await world.session.commit()
    await world.scheduler().tick_admin_reminders()
    assert world.tg.chats_of(BOT(1)) == [OWNER(1)]
    assert "позвонить поставщику" in world.tg.texts_of(BOT(1))[0] and "🌙 Тихий режим" in world.tg.texts_of(BOT(1))[0]
    assert await deferred(world.session, 1) == []


async def test_master_reminder_2h_in_quiet_time_is_deferred_and_expires_with_booking(world):  # noqa: F811
    await world.add_master(1, tz=TZ_NAME)
    start, end = around_now(inside=True)
    await QuietModeRepository(world.session, 1).save(enabled=True, start_time=start, end_time=end)
    await world.session.commit()
    booking = await world.add_due_booking(1, kinds=("reminder_2h_master", "reminder_2h_client"))

    await world.scheduler().tick_notifications()
    assert world.tg.chats_of(BOT(1)) == [world_client(1)]  # клиенту напоминание ушло, мастеру — нет
    assert await world.statuses(1) == ["sent", "sent"]
    [row] = await deferred(world.session, 1)
    await world.session.refresh(booking)
    assert row.expires_at == booking.start_datetime and "Через 2 часа" in row.text


def world_client(master_id: int) -> int:
    from tests.test_stage6_scheduler import CLIENT

    return CLIENT(master_id)


async def test_client_messages_are_not_affected_by_quiet_mode(world):  # noqa: F811
    await world.add_master(1, tz=TZ_NAME)
    start, end = around_now(inside=True)
    await QuietModeRepository(world.session, 1).save(enabled=True, start_time=start, end_time=end)
    await world.session.commit()
    await world.add_due_booking(1, kinds=("reminder_24h",))
    await world.scheduler().tick_notifications()
    assert world.tg.chats_of(BOT(1)) == [world_client(1)]
    assert await deferred(world.session, 1) == []


# ---------------------------------------------------------------- через настоящий хендлер


async def test_handler_defers_notice_in_quiet_time_and_sends_it_otherwise(session):
    from aiogram.enums import ChatMemberStatus

    from app.bot.handlers.client import bot_blocked
    from tests.test_bot_blocked import CLIENT_ID, _update

    start, end = around_now(inside=True)
    await setup_master(session, start=start, end=end)
    async with tg_harness(bot_blocked.router) as h:
        await h.dp.feed_update(h.bot, _update(CLIENT_ID, ChatMemberStatus.KICKED))
        assert h.tg.sent_to(ADMIN) == []
    [row] = await deferred(session)
    assert "заблокировал бота" in row.text

    other_start, other_end = around_now(inside=False)
    await QuietModeRepository(session, MASTER_ID).save(enabled=True, start_time=other_start, end_time=other_end)
    await session.commit()
    async with tg_harness(bot_blocked.router) as h:
        await h.dp.feed_update(h.bot, _update(CLIENT_ID, ChatMemberStatus.KICKED))
        assert len(h.tg.sent_to(ADMIN)) == 1
    assert len(await deferred(session)) == 1


# ---------------------------------------------------------------- экран в «⚙️ Настройки»


def cb(action: str, field: str = "none", value: str = "none") -> AdminSettingsCallback:
    return AdminSettingsCallback(action=action, field=field, value=value)


async def quiet_row(session) -> MasterQuietMode | None:
    session.expire_all()
    return (await session.execute(select(MasterQuietMode).where(MasterQuietMode.master_id == MASTER_ID))).scalar_one_or_none()


def last_markup(h) -> InlineKeyboardMarkup:
    """Клавиатура последнего отредактированного сообщения."""
    return [m for m in h.tg.requests if isinstance(m, EditMessageText)][-1].reply_markup


def button_texts(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


async def test_quiet_screen_has_toggle_and_two_time_buttons_only(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("menu", "quiet"))
        assert "Тихий режим" in h.tg.edited()[-1] and "выключен" in h.tg.edited()[-1]
        assert await quiet_row(session) is None  # просмотр ничего не пишет
        # готовых окон нет: мастер сам выбирает начало и конец
        assert button_texts(last_markup(h)) == [["🟢 Включить"], ["🕙 Начало: 22:00", "🕗 Конец: 08:00"], ["⬅️ Назад"]]

        await h.press(ADMIN, cb("quiet", "toggle"))
        row = await quiet_row(session)
        assert row.enabled and (row.start_time, row.end_time) == (T(22), T(8))
        assert "включён" in h.tg.edited()[-1]
        assert button_texts(last_markup(h))[0] == ["⚪ Выключить"]

        await h.press(ADMIN, cb("quiet", "toggle"))
        assert (await quiet_row(session)).enabled is False


async def test_quiet_hour_grids_start_in_the_evening_and_in_the_morning(session, master_profile):
    await set_owner(session, ADMIN)
    await QuietModeRepository(session, MASTER_ID).save(enabled=False, start_time=T(22), end_time=T(8))
    await session.commit()
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("quiet", "ask", "start"))
        assert await h.get_state(ADMIN) is None  # ввода текстом нет: время выбирается только кнопками
        assert "Сейчас: 22:00" in h.tg.edited()[-1]
        markup = last_markup(h)
        assert button_texts(markup) == [
            ["18", "19", "20", "21", "✅ 22", "23"],
            ["00", "01", "02", "03", "04", "05"],
            ["06", "07", "·", "09", "10", "11"],  # 08 — уже выбранный конец
            ["12", "13", "14", "15", "16", "17"],
            ["✖ Отмена"],
        ]
        assert markup.inline_keyboard[0][4].callback_data == cb("quiet", "start", "22").pack()
        assert markup.inline_keyboard[4][0].callback_data == cb("menu", "quiet").pack()

        await h.press(ADMIN, cb("quiet", "ask", "end"))
        assert "Сейчас: 08:00" in h.tg.edited()[-1]
        markup = last_markup(h)
        assert button_texts(markup) == [
            ["05", "06", "07", "✅ 08", "09", "10"],
            ["11", "12", "13", "14", "15", "16"],
            ["17", "18", "19", "20", "21", "·"],  # 22 — уже выбранное начало
            ["23", "00", "01", "02", "03", "04"],
            ["✖ Отмена"],
        ]
        assert markup.inline_keyboard[0][3].callback_data == cb("quiet", "end", "08").pack()


async def test_quiet_pick_hours_saves_window_and_keeps_enabled_flag(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("quiet", "start", "21"))
        row = await quiet_row(session)
        assert (row.start_time, row.end_time, row.enabled) == (T(21), T(8), False)
        assert "21:00–08:00" in h.tg.edited()[-1] and "выключен" in h.tg.edited()[-1]

        await h.press(ADMIN, cb("quiet", "end", "07"))
        row = await quiet_row(session)
        assert (row.start_time, row.end_time, row.enabled) == (T(21), T(7), False)

        await h.press(ADMIN, cb("quiet", "toggle"))
        await h.press(ADMIN, cb("quiet", "end", "09"))
        row = await quiet_row(session)
        assert (row.start_time, row.end_time, row.enabled) == (T(21), T(9), True)  # выбор часа режим не выключает

        await h.press(ADMIN, cb("quiet", "end", "09"))  # тот же час повторно (устаревшая сетка) — без ошибок
        assert (await quiet_row(session)).end_time == T(9)


async def test_quiet_pick_rejects_same_hour_and_stale_or_forged_buttons(session, master_profile):
    await set_owner(session, ADMIN)
    await QuietModeRepository(session, MASTER_ID).save(enabled=True, start_time=T(22), end_time=T(8))
    await session.commit()
    stale = [
        ("start", "24"),
        ("start", "-1"),
        ("end", "abc"),
        ("end", "٣"),
        ("end", ""),
        ("preset", "23000900"),  # кнопка готового окна из старого сообщения
        ("ask", "middle"),
        ("bogus", "22"),
    ]
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("quiet", "end", "22"))  # конец совпал бы с началом
        await h.press(ADMIN, cb("quiet", "start", "08"))  # начало совпало бы с концом
        assert all("не должны совпадать" in alert for alert in h.tg.alerts())
        assert len(h.tg.alerts()) == 2
        for field, value in stale:
            await h.press(ADMIN, cb("quiet", field, value))
        assert h.tg.alerts()[2:] == [admin_settings.STALE] * len(stale)
    row = await quiet_row(session)
    assert (row.start_time, row.end_time, row.enabled) == (T(22), T(8), True)  # ничего не изменилось


async def test_quiet_cancel_returns_to_quiet_screen_and_drops_stale_input(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.set_state(ADMIN, AdminSettingsFSM.enter_currency)  # мастер бросил ввод другого экрана
        await h.press(ADMIN, cb("quiet", "ask", "start"))
        assert await h.get_state(ADMIN) is None
        await h.press(ADMIN, cb("menu", "quiet"))  # callback кнопки «✖ Отмена»
        assert "Тихий режим" in h.tg.edited()[-1]
        assert button_texts(last_markup(h))[0] == ["🟢 Включить"]
        assert await quiet_row(session) is None  # отмена ничего не сохраняет


async def test_main_settings_screen_shows_quiet_mode_state(session, master_profile):
    await set_owner(session, ADMIN)
    await QuietModeRepository(session, MASTER_ID).save(enabled=True, start_time=T(22), end_time=T(8))
    await session.commit()
    async with tg_harness(admin_settings.router) as h:
        await h.press(ADMIN, cb("menu", "main"))
        assert "Тихий режим: 🟢 22:00–08:00" in h.tg.edited()[-1]


async def test_quiet_screen_is_owner_only(session, master_profile):
    await set_owner(session, ADMIN)
    async with tg_harness(admin_settings.router) as h:
        await h.press(555, cb("quiet", "toggle"))
        await h.press(555, cb("quiet", "ask", "start"))
        await h.press(555, cb("quiet", "start", "21"))
    assert await quiet_row(session) is None
    assert h.tg.edited() == []
