"""Удобство суперадмин-бота: компактная карточка и подменю «🛠 Ещё», «Включить бота» только у выключенного бота, поиск мастера,
журнал по мастеру, кнопка исправления webhook, подтверждение сброса к эталону (числа, предупреждение, дата) и дата эталона."""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Update
from sqlalchemy import delete, select, update

from app.config.settings import settings
from app.database.models import Booking, BookingStatus, MasterBot, ReferenceSnapshot, SuperadminAction
from app.database.session import make_session_factory
from app.multibot.management import BotCheck
from app.superadmin import service as sa
from app.superadmin.access import SuperadminAccessMiddleware
from app.superadmin.handlers import build_router as build_superadmin_router
from app.superadmin.service import MasterRow
from app.superadmin.ui import BTN_MASTERS, SaCallback, _fmt_dt, card_keyboard, check_keyboard, journal_text, more_keyboard
from tests.conftest import TEST_DATABASE_URL
from tests.stage11_support import A, B, BOT_ID, OWNER, two_masters  # noqa: F401  (фикстура pytest)
from tests.tg import RecordingSession
from tests.webhook_utils import message_update

SUPERADMIN_ID = 424242


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _cb(action: str, master_id: int = 0) -> str:
    return SaCallback(action=action, master_id=master_id).pack()


@pytest_asyncio.fixture
async def factory(session, two_masters):
    f = make_session_factory(TEST_DATABASE_URL)
    yield f
    async with f() as s:
        await s.execute(delete(SuperadminAction))
        await s.execute(delete(ReferenceSnapshot))
        await s.commit()
    await f.kw["bind"].dispose()


@pytest_asyncio.fixture
async def panel(factory):
    """Настоящий диспетчер суперадмин-бота с Telegram-заглушкой."""
    tg = RecordingSession()
    bot = Bot("777000:" + "S" * 35, session=tg)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware.register(SuperadminAccessMiddleware(frozenset({SUPERADMIN_ID})))
    dp.include_router(build_superadmin_router())
    dp.workflow_data.update(session_factory=factory, registry=None, health_monitor=None, sa_bot_id=777000)
    counter = iter(range(1, 10_000))

    async def feed(raw: dict) -> None:
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    async def press(action: str, master_id: int = 0) -> None:
        raw = {
            "update_id": next(counter),
            "callback_query": {
                "id": str(next(counter)),
                "from": {"id": SUPERADMIN_ID, "is_bot": False, "first_name": "sa"},
                "chat_instance": "x",
                "data": _cb(action, master_id),
                "message": {
                    "message_id": 1,
                    "date": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                    "chat": {"id": SUPERADMIN_ID, "type": "private"},
                    "text": "card",
                },
            },
        }
        await feed(raw)

    async def say(text: str) -> None:
        await feed(message_update(next(counter), SUPERADMIN_ID, text))

    def last_markup():
        shown = [m for m in tg.requests if isinstance(m, (SendMessage, EditMessageText))]
        return shown[-1].reply_markup

    return SimpleNamespace(press=press, say=say, tg=tg, last_markup=last_markup)


async def _save_reference(factory) -> dt.datetime:
    """Эталон нужен только как «сохранённая запись»: подтверждению сброса его содержимое не требуется."""
    async with factory() as s:
        s.add(ReferenceSnapshot(source_master_id=A, payload={}, created_by_telegram_id=SUPERADMIN_ID))
        await s.commit()
    async with factory() as s:
        return (await s.execute(select(ReferenceSnapshot.created_at))).scalar_one()


# ---- карточка и подменю «Ещё» ----


async def test_card_keeps_only_everyday_buttons(session, two_masters, factory):
    flat = _callbacks(card_keyboard(await sa.get_master_card(factory, B)))
    for action in ("check", "extc", "ask_md", "ask_rs", "mj", "more", "list"):
        assert _cb(action, B) in flat or _cb(action) in flat, action
    for action in ("wh", "whrot", "chown", "ask_rm", "ask_bd", "do_bd"):  # редкое — в «Ещё»; ручного выключения бота нет
        assert _cb(action, B) not in flat, action


async def test_more_menu_holds_rare_actions(session, two_masters, factory):
    flat = _callbacks(more_keyboard(await sa.get_master_card(factory, B)))
    assert [_cb(a, B) for a in ("wh", "whrot", "chown", "ask_rm", "card")] == [c for c in flat]

    async with factory() as s:
        await s.execute(delete(MasterBot).where(MasterBot.master_id == B))
        await s.commit()
    without_bot = _callbacks(more_keyboard(await sa.get_master_card(factory, B)))
    assert without_bot == [_cb("chown", B), _cb("card", B)]  # webhook и удалять нечего


async def test_more_button_opens_the_submenu(session, two_masters, factory, panel):
    await panel.press("more", B)
    assert any("Дополнительные действия · мастер №2" in t for t in panel.tg.texts())
    assert _cb("chown", B) in _callbacks(panel.last_markup())


# ---- «Включить бота» только у выключенного бота ----


async def test_enable_bot_button_only_when_bot_is_off(session, two_masters, factory):
    flat = _callbacks(card_keyboard(await sa.get_master_card(factory, B)))
    assert _cb("en_b", B) not in flat

    await sa.set_bot_active(factory, B, False)
    card = await sa.get_master_card(factory, B)
    assert _cb("en_b", B) in _callbacks(card_keyboard(card))
    assert "▶️ Включить бота" in _labels(card_keyboard(card))


async def test_enable_bot_flow(session, two_masters, factory, panel):
    await sa.set_bot_active(factory, B, False)
    await panel.press("en_b", B)
    session.expire_all()
    assert (await session.execute(select(MasterBot.is_active).where(MasterBot.master_id == B))).scalar_one() is True
    assert any("Бот включён" in t for t in panel.tg.texts())
    assert any(a.action == "enable_bot" and a.target_master_id == B for a in await sa.recent_actions(factory))


async def test_old_disable_bot_buttons_do_nothing(session, two_masters, factory, panel):
    """В чате могла остаться старая карточка: её «Выключить бота» бота не выключает."""
    await panel.press("ask_bd", B)
    await panel.press("do_bd", B)
    session.expire_all()
    assert (await session.execute(select(MasterBot.is_active).where(MasterBot.master_id == B))).scalar_one() is True
    assert panel.tg.texts() == []


# ---- поиск мастера ----


def _row(master_id: int, owner: int | None, bot_id: int | None, username: str | None) -> MasterRow:
    return MasterRow(master_id=master_id, owner_telegram_id=owner, is_active=True, telegram_bot_id=bot_id, bot_username=username, bot_active=True)


ROWS = [_row(1, 5551234567, 8100000001, "Anna_Beauty_bot"), _row(2, 5559876543, 8200000002, "nails_bot"), _row(3, None, None, None)]


def test_match_masters_by_number_owner_id_and_bot_id():
    assert [r.master_id for r in sa.match_masters(ROWS, "2")] == [2]
    assert [r.master_id for r in sa.match_masters(ROWS, " 5551234567 ")] == [1]
    assert [r.master_id for r in sa.match_masters(ROWS, "8200000002")] == [2]
    assert sa.match_masters(ROWS, "5551234") == []  # число — только точное совпадение


def test_match_masters_by_part_of_bot_username():
    assert [r.master_id for r in sa.match_masters(ROWS, "@annabeauty")] == []  # подстрока, а не «похоже»
    assert [r.master_id for r in sa.match_masters(ROWS, "@ANNA_beauty")] == [1]
    assert [r.master_id for r in sa.match_masters(ROWS, "nails")] == [2]
    assert [r.master_id for r in sa.match_masters(ROWS, "_bot")] == [1, 2]  # мастер без бота не попадает


def test_match_masters_ignores_empty_query():
    assert sa.match_masters(ROWS, "") == [] and sa.match_masters(ROWS, "  @ ") == []


async def test_list_has_search_button(session, two_masters, factory, panel):
    await panel.say(BTN_MASTERS)
    assert _cb("find") in _callbacks(panel.last_markup())


async def test_search_flow_finds_master_by_bot_username(session, two_masters, factory, panel):
    await panel.press("find")
    assert any("Поиск мастера" in t for t in panel.tg.texts())

    await panel.say("@BOT82000")
    assert any("найдено: 1" in t and "№2" in t and "№1 " not in t for t in panel.tg.texts()[-1:])
    assert _cb("card", B) in _callbacks(panel.last_markup())
    assert _cb("card", A) not in _callbacks(panel.last_markup())
    assert _cb("find") in _callbacks(panel.last_markup())  # можно искать снова


async def test_search_flow_finds_master_by_owner_id_and_reports_no_match(session, two_masters, factory, panel):
    await panel.press("find")
    await panel.say(str(OWNER[A]))
    assert _cb("card", A) in _callbacks(panel.last_markup())

    await panel.press("find")
    await panel.say("совсем-не-то")
    assert "мастеров не найдено" in panel.tg.texts()[-1]
    assert _callbacks(panel.last_markup()) == [_cb("find"), _cb("list")]


async def test_search_by_bot_id_goes_through_digits(session, two_masters, factory, panel):
    await panel.press("find")
    await panel.say(str(BOT_ID[B]))
    assert _cb("card", B) in _callbacks(panel.last_markup())


async def test_search_rejects_too_long_query_and_keeps_waiting(session, two_masters, factory, panel):
    await panel.press("find")
    await panel.say("x" * 65)
    assert "до 64 символов" in panel.tg.texts()[-1]
    await panel.say(str(OWNER[B]))  # состояние сохранилось: следующий запрос обрабатывается
    assert _cb("card", B) in _callbacks(panel.last_markup())


async def test_search_state_ends_after_result_and_on_menu_button(session, two_masters, factory, panel):
    await panel.press("find")
    await panel.say(str(OWNER[A]))
    shown = len(panel.tg.texts())
    await panel.say(str(OWNER[B]))  # без новой кнопки поиска обычный текст ничего не запускает
    assert len(panel.tg.texts()) == shown

    await panel.press("find")
    await panel.say(BTN_MASTERS)  # кнопка меню выводит из поиска и открывает список
    shown = len(panel.tg.texts())
    await panel.say(str(OWNER[B]))
    assert len(panel.tg.texts()) == shown


async def test_search_shows_only_first_page_of_matches(session, two_masters, factory, panel):
    many = [_row(n, n, n, f"bot{n}_bot") for n in range(1, 12)]
    from app.superadmin.ui import PAGE_SIZE, search_keyboard, search_text

    text = search_text("bot", sa.match_masters(many, "bot"), {})
    assert "найдено: 11" in text and f"Показаны первые {PAGE_SIZE}" in text
    assert len(_callbacks(search_keyboard(many))) == PAGE_SIZE + 2  # мастера + «новый поиск» + «все мастера»


# ---- журнал по мастеру ----


async def _log_some_actions(factory) -> None:
    await sa.log_action(factory, actor_telegram_id=SUPERADMIN_ID, action="extend_trial", master_id=A, detail="+7 дн.")
    await sa.log_action(factory, actor_telegram_id=SUPERADMIN_ID, action="clear_trial", master_id=B)
    await sa.log_action(factory, actor_telegram_id=SUPERADMIN_ID, action="check_bot", master_id=A, ok=False, detail="ошибка")


async def test_recent_actions_can_be_limited_to_one_master(session, two_masters, factory):
    await _log_some_actions(factory)
    assert {a.action for a in await sa.recent_actions(factory, master_id=A)} == {"extend_trial", "check_bot"}
    assert [a.action for a in await sa.recent_actions(factory, master_id=B)] == ["clear_trial"]
    assert len(await sa.recent_actions(factory)) == 3  # общий журнал по-прежнему общий


async def test_journal_text_for_a_master_does_not_repeat_master_number(session, two_masters, factory):
    await _log_some_actions(factory)
    text = journal_text(await sa.recent_actions(factory, master_id=A), master_id=A)
    assert text.splitlines()[0] == "📜 Последние действия · мастер №1"
    assert text.count("мастер №1") == 1
    assert journal_text([], master_id=A) == "📜 Журнал · мастер №1 пуст."
    assert journal_text([]) == "📜 Журнал пуст."


async def test_card_journal_button_shows_only_that_masters_actions(session, two_masters, factory, panel):
    await _log_some_actions(factory)
    await panel.press("mj", A)
    shown = panel.tg.texts()[-1]
    assert "срок продлён" in shown and "проверка бота" in shown and "срок снят" not in shown
    assert _callbacks(panel.last_markup()) == [_cb("card", A)]

    await panel.press("mj", 999)
    assert panel.tg.texts()[-1] == "📜 Журнал · мастер №999 пуст."


# ---- «Исправить webhook» под результатом проверки ----


def _check(**kw) -> BotCheck:
    base = dict(master_id=B, telegram_bot_id=BOT_ID[B], is_active=True, token_valid=True, webhook_registered=True, webhook_matches=True)
    return BotCheck(**{**base, **kw})


@pytest.fixture
def webhook_mode(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "webhook")


def test_check_keyboard_offers_fix_when_webhook_is_missing_or_wrong(webhook_mode):
    for broken in (dict(webhook_registered=False, webhook_matches=None), dict(webhook_matches=False)):
        assert _callbacks(check_keyboard(_check(**broken))) == [_cb("wh", B), _cb("card", B)]


def test_check_keyboard_has_no_fix_when_nothing_to_fix(webhook_mode):
    assert _callbacks(check_keyboard(_check())) == [_cb("card", B)]  # webhook в порядке
    assert _callbacks(check_keyboard(_check(webhook_matches=None))) == [_cb("card", B)]  # адрес не сверен — исправлять нечего
    assert _callbacks(check_keyboard(_check(token_valid=False, webhook_registered=None))) == [_cb("card", B)]  # сначала токен
    assert _callbacks(check_keyboard(_check(error="нет связи", token_valid=None, webhook_registered=None))) == [_cb("card", B)]


def test_check_keyboard_has_no_fix_in_polling_mode(monkeypatch):
    monkeypatch.setattr(settings, "run_mode", "polling")
    assert _callbacks(check_keyboard(_check(webhook_registered=False, webhook_matches=None))) == [_cb("card", B)]


# ---- подтверждение «Сбросить к эталону» ----


async def test_reset_confirm_says_what_will_be_deleted_and_warns_about_upcoming_bookings(session, two_masters, factory, panel):
    saved_at = await _save_reference(factory)
    await panel.press("ask_rs", B)
    text = panel.tg.texts()[-1]
    assert "Сбросить мастера №2 к эталону?" in text
    assert "записей — 4 (подтверждённых 3), клиентов — 3" in text
    assert f"сохранён {_fmt_dt(saved_at)}" in text
    assert "будущие подтверждённые записи: 3" in text and "клиенты об этом не узнают" in text
    assert [b.text for row in panel.last_markup().inline_keyboard for b in row][0] == "Да, удалить записи и сбросить"
    assert _cb("do_rs", B) in _callbacks(panel.last_markup())


async def test_reset_confirm_has_no_warning_without_upcoming_bookings(session, two_masters, factory, panel):
    await _save_reference(factory)
    async with factory() as s:
        await s.execute(update(Booking).where(Booking.master_id == B).values(status=BookingStatus.CANCELLED.value))
        await s.commit()
    await panel.press("ask_rs", B)
    text = panel.tg.texts()[-1]
    assert "⚠️" not in text and "записей — 4 (подтверждённых 0)" in text
    assert "Да, сбросить к эталону" in _labels(panel.last_markup())


async def test_reset_confirm_without_reference_offers_no_reset(session, two_masters, factory, panel):
    await panel.press("ask_rs", B)
    assert "Эталон ещё не сохранён" in panel.tg.texts()[-1] and "Сохранить как эталон" in panel.tg.texts()[-1]
    assert _callbacks(panel.last_markup()) == [_cb("card", B)]


async def test_overwrite_confirm_shows_the_current_reference_date(session, two_masters, factory, panel):
    await panel.press("ask_ow", A)
    assert "Действующий эталон: ещё не сохранялся" in panel.tg.texts()[-1]
    saved_at = await _save_reference(factory)
    await panel.press("ask_ow", A)
    assert f"Действующий эталон: сохранён {_fmt_dt(saved_at)}" in panel.tg.texts()[-1]


# ---- дата эталона в карточке мастера №1 ----


async def test_reference_card_shows_when_reference_was_saved(session, two_masters, factory, panel):
    await panel.press("card", A)
    assert "Эталон: ещё не сохранён" in panel.tg.texts()[-1]
    saved_at = await _save_reference(factory)
    await panel.press("card", A)
    assert f"Эталон: сохранён {_fmt_dt(saved_at)}" in panel.tg.texts()[-1]


async def test_other_master_card_has_no_reference_line(session, two_masters, factory, panel):
    await _save_reference(factory)
    await panel.press("card", B)
    assert "Эталон:" not in panel.tg.texts()[-1]


@pytest.mark.parametrize("master_id", [A, B])
async def test_card_shows_upcoming_bookings_count_for_confirmation(session, two_masters, factory, master_id):
    card = await sa.get_master_card(factory, master_id)
    assert (card.bookings, card.bookings_confirmed, card.bookings_upcoming) == (4, 3, 3)


async def test_past_confirmed_booking_is_not_counted_as_upcoming(session, two_masters, factory):
    """Предупреждение о будущих записях не должно срабатывать на давно прошедшую запись, которую не успели закрыть."""
    async with factory() as s:
        await s.execute(
            update(Booking).where(Booking.master_id == B, Booking.status == BookingStatus.COMPLETED.value).values(status=BookingStatus.CONFIRMED.value)
        )
        await s.commit()
    card = await sa.get_master_card(factory, B)
    assert (card.bookings_confirmed, card.bookings_upcoming) == (4, 3)
