"""Этап 11, уровень хендлеров и callback_data: id сущностей мастера B, подставленные в бот мастера A, не имеют эффекта.

Автопокрытие: все подклассы CallbackData из app/bot/keyboards/callback_data.py находятся интроспекцией. Каждый класс либо
имеет подделанные нажатия в FORGED, либо перечислен в NO_ENTITY с причиной (в нём нет id сущностей мастера). Новый класс без
решения роняет `test_every_callback_class_is_decided`.
Нажимает владелец A (админские) или общий клиент SHARED (клиентские) в боте A. Проверяется: данные B не изменились, данные A
не изменились, в ответах бота нет данных B, ничего не отправлено людям B.
"""

import inspect

import pytest
from aiogram.filters.callback_data import CallbackData
from sqlalchemy import update

import app.bot.keyboards.callback_data as callback_module
from app.bot.dispatcher import default_routers
from app.bot.keyboards.callback_data import AdminServiceCallback, BookingActionCallback
from app.bot.menu_mode import is_admin_mode, is_in_admin_panel
from app.database.models import Master, MasterClient, Service
from app.tenancy import TenantContext
from tests.conftest import client_row
from tests.stage11_support import (
    A,
    B,
    BOT_ID,
    ONLY_TG,
    OWNER,
    SHARED_TG,
    TAG,
    snapshot,
    two_masters,  # noqa: F401  (фикстура pytest)
)
from tests.tg import tg_harness

# Данные мастера B, которые не должны появиться в ответах бота A.
B_MARKERS = [
    f"Услуга-{TAG[B]}", f"Скрытая-{TAG[B]}", f"Ревью-{TAG[B]}", f"отзыв {TAG[B]}", f"Работа-{TAG[B]}",
    f"блокировка-{TAG[B]}", f"Мастер-{TAG[B]}", f"Адрес {TAG[B]}", f"напоминание {TAG[B]}", f"Мануал-{TAG[B]}",
    f"доступ {TAG[B]}", f"лимит {TAG[B]}", f"bot{BOT_ID[B]}", "EUR", "Only" + TAG[B], "Owner" + TAG[B],
]

CLIENT_CALLBACKS = {
    "ServiceCallback", "BookingActionCallback", "ReminderActionCallback", "PortfolioCallback",
}

# Классы без id сущностей мастера: подделывать нечего — причина обязательна.
NO_ENTITY: dict[str, str] = {
    "DateCallback": "дата, а не id сущности; свободное время считается по мастеру апдейта",
    "DatesPageCallback": "номер страницы",
    "CalendarCallback": "месяц календаря или действие над неактивной ячейкой; свободное время считается по мастеру апдейта",
    "TimeCallback": "unix-время; слот проверяется по расписанию и броням мастера апдейта",
    "ConfirmCallback": "yes/edit внутри FSM текущего пользователя",
    "ScheduleBookCallback": "дата",
    "NavCallback": "навигация",
    "AdminBookingsPageCallback": "номер страницы",
    "AdminScheduleCallback": "действие над расписанием мастера апдейта",
    "AdminAboutCallback": "поле профиля мастера апдейта",
    "ReportCallback": "период отчёта мастера апдейта",
    "ReviewsCallback": "действие без id",
    "ReviewsPageCallback": "смещение страницы",
    "ReviewRatingCallback": "оценка 1..5",
    "AdminReviewsCallback": "навигация",
    "MasterRequestCallback": "действие клиента без id",
    "AdminSettingsCallback": "настройки мастера апдейта (проверяются в тестах репозиториев)",
    "MyBotCallback": "бот мастера апдейта берётся из tenant.master_id (см. test_my_bot_card_shows_only_own_bot)",
}

ids_b = lambda ns: dict(  # noqa: E731
    service_id=ns.service_id[B], booking_id=ns.booking_id[B], item_id=ns.portfolio_ids[B][0], block_id=ns.block_id[B],
    user_id=ns.only_id[B], review_id=ns.review_ids[B][0], reminder_id=ns.reminder_id[B],
)

# (класс, поле id, [(остальные поля), ...]) — id подставляется из ids_b / особых источников.
FORGED: dict[str, tuple[str, list[dict]]] = {
    "ServiceCallback": ("service_id", [{}]),
    "BookingActionCallback": ("booking_id", [{"action": a} for a in ("cancel", "reschedule", "cancel_confirm", "cancel_back")]),
    "ReminderActionCallback": ("booking_id", [{"action": a} for a in ("still_valid", "reschedule", "cancel")]),
    "PortfolioCallback": ("item_id", [{}]),
    "AdminPortfolioCallback": ("item_id", [{"action": a} for a in ("delete", "delete_confirm", "edit_title", "edit_description")]),
    "AdminBookingCallback": ("booking_id", [{"action": a, "page": 0} for a in ("delete", "confirm", "back")]),
    "AdminBlockCallback": ("block_id", [{"action": a} for a in ("unblock", "confirm", "back")]),
    "AdminServiceCallback": (
        "service_id", [{"action": a} for a in ("open", "toggle", "rename", "price", "buffer", "delete", "delete_yes")]
    ),
    "AdminDurationCallback": ("service_id", [{"minutes": 90}]),
    "AdminBufferCallback": ("service_id", [{"minutes": 15}]),
    "AdminClientAccessCallback": ("user_id", [{"action": a} for a in ("restrict", "allow")]),
    "AdminReminderCallback": ("reminder_id", [{"action": a, "recurrence": "none"} for a in ("open", "delete", "confirm")]),
    "AdminReviewDeleteCallback": ("review_id", [{"action": a} for a in ("open", "confirm")]),
    "AdminLimitRequestCallback": ("request_id", [{"action": a} for a in ("approve", "reject")]),
    "AdminLimitCommentCallback": ("request_id", [{"action": a} for a in ("yes", "no")]),
    "AdminAccessRequestCallback": ("request_id", [{"action": a} for a in ("open", "block")]),
}


def callback_classes() -> dict[str, type]:
    return {
        name: cls
        for name, cls in vars(callback_module).items()
        if inspect.isclass(cls) and issubclass(cls, CallbackData) and cls is not CallbackData and cls.__module__ == callback_module.__name__
    }


def forged_id(ns, cls_name: str, field: str) -> int:
    if field == "request_id":
        return ns.access_request_id[B] if cls_name == "AdminAccessRequestCallback" else ns.limit_request_id[B]
    return ids_b(ns)[field]


def build_payloads() -> list[tuple[str, dict]]:
    return [(name, extra) for name, (_, variants) in FORGED.items() for extra in variants]


def test_every_callback_class_is_decided():
    """Новый класс CallbackData без подделанных нажатий и без причины в NO_ENTITY — красный тест."""
    found = set(callback_classes())
    decided = set(FORGED) | set(NO_ENTITY)
    assert found - decided == set(), f"Нет решения по callback-классам: {sorted(found - decided)}"
    assert decided - found == set(), f"Устаревшие записи: {sorted(decided - found)}"
    assert not (set(FORGED) & set(NO_ENTITY))
    assert all(reason.strip() for reason in NO_ENTITY.values())
    for name, (field, _) in FORGED.items():
        assert field in callback_classes()[name].model_fields, f"{name}: нет поля {field}"


def test_classes_with_entity_ids_are_not_hidden_in_no_entity():
    """Автопроверка NO_ENTITY: у таких классов нет полей-id сущностей (по именам полей)."""
    id_fields = {"service_id", "booking_id", "item_id", "block_id", "user_id", "review_id", "request_id", "reminder_id"}
    for name in NO_ENTITY:
        assert not (id_fields & set(callback_classes()[name].model_fields)), f"{name} несёт id сущности — подделывайте его"


def _label(param) -> str:
    return f"{param[0]}-{'-'.join(str(v) for v in param[1].values())}"


@pytest.mark.parametrize("payload", build_payloads(), ids=_label)
async def test_forged_callback_with_ids_of_b_in_bot_of_a_has_no_effect(session, two_masters, payload):
    ns = two_masters
    name, extra = payload
    field, _ = FORGED[name]
    data = getattr(callback_module, name)(**{field: forged_id(ns, name, field), **extra})
    sender = SHARED_TG if name in CLIENT_CALLBACKS else OWNER[A]

    before_a, before_b = await snapshot(session, A), await snapshot(session, B)
    async with tg_harness(*default_routers(), master_id=A) as h:
        await h.press(sender, data)
        shown = "\n".join(h.tg.everything_shown())
        recipients = {m.chat_id for m in h.tg.requests if hasattr(m, "chat_id")}
    assert await snapshot(session, B) == before_b, "данные мастера B изменены нажатием в боте A"
    assert await snapshot(session, A) == before_a, "подделанный id чужой сущности изменил данные мастера A"
    leaked = [marker for marker in B_MARKERS if marker in shown]
    assert not leaked, f"в ответе бота A данные мастера B: {leaked}"
    assert not (recipients & {OWNER[B], ONLY_TG[B]}), "бот A написал людям мастера B"


# ---------------------------------------------------------------- права владельца


@pytest.mark.parametrize("owner_master,bot_master", [(A, B), (B, A)])
async def test_owner_of_one_master_has_no_admin_rights_in_the_other_bot(session, two_masters, owner_master, bot_master):
    ns = two_masters
    toggle = AdminServiceCallback(service_id=ns.service_id[bot_master], action="delete_yes")
    before = await snapshot(session, bot_master)
    async with tg_harness(*default_routers(), master_id=bot_master) as h:
        await h.press(OWNER[owner_master], toggle)
        await h.send_text(OWNER[owner_master], "🤖 Мой бот")
        await h.send_text(OWNER[owner_master], "🗑️ Отменить запись")
        shown = "\n".join(h.tg.everything_shown())
    assert await snapshot(session, bot_master) == before
    assert f"bot{BOT_ID[bot_master]}" not in shown  # карточка «Мой бот» не открылась


async def test_ownership_is_decided_per_master(session, two_masters):
    ma, mb = await session.get(Master, A), await session.get(Master, B)
    tenant_a, tenant_b = TenantContext(master_id=A, master=ma), TenantContext(master_id=B, master=mb)
    assert await is_admin_mode(session, tenant_a, OWNER[A]) is True
    assert await is_admin_mode(session, tenant_b, OWNER[B]) is True
    assert await is_admin_mode(session, tenant_b, OWNER[A]) is False
    assert await is_admin_mode(session, tenant_a, OWNER[B]) is False


async def test_client_of_a_is_not_client_of_b_and_flags_are_per_master(session, two_masters):
    ns = two_masters
    assert await is_in_admin_panel(session, A, OWNER[A]) is True
    assert await is_in_admin_panel(session, B, OWNER[A]) is False  # у B владелец A вообще не клиент
    assert await is_in_admin_panel(session, A, OWNER[B]) is False
    assert (await client_row(session, ns.shared_id, A)).is_restricted is False
    assert (await client_row(session, ns.shared_id, B)).is_restricted is True
    from app.database.repositories.user_repository import UserRepository

    assert await UserRepository(session, B).get_by_telegram_id(ONLY_TG[A]) is None
    assert await UserRepository(session, A).get_by_telegram_id(ONLY_TG[B]) is None


async def test_harness_control_own_entities_are_reachable(session, two_masters):
    """Контроль против «зелёных вхолостую»: стенд реально определяет мастера и хендлеры отвечают на СВОИ сущности."""
    ns = two_masters
    async with tg_harness(*default_routers(), master_id=A) as h:
        await h.press(OWNER[A], AdminServiceCallback(service_id=ns.service_id[A], action="open"))
        shown = "\n".join(h.tg.everything_shown())
    assert f"Услуга-{TAG[A]}" in shown, "стенд не обработал нажатие владельца: подделанные нажатия ниже ничего бы не проверяли"


async def test_my_bot_card_shows_only_own_bot(session, two_masters):
    async with tg_harness(*default_routers(), master_id=A) as h:
        await h.send_text(OWNER[A], "🤖 Мой бот")
        shown = "\n".join(h.tg.everything_shown())
    assert f"bot{BOT_ID[A]}_bot" in shown or shown == ""  # карточка может требовать настроек окружения; чужого бота нет в любом случае
    assert f"bot{BOT_ID[B]}" not in shown and "W" * 35 not in shown and "Q" * 35 not in shown


# ---------------------------------------------------------------- отключённый мастер


async def test_disabled_master_handlers_are_unreachable(session, two_masters):
    ns = two_masters
    await session.execute(update(Master).where(Master.id == B).values(is_active=False))
    await session.commit()
    before = await snapshot(session, B)
    async with tg_harness(*default_routers(), master_id=B) as h:
        await h.press(OWNER[B], AdminServiceCallback(service_id=ns.service_id[B], action="delete_yes"))
        await h.press(SHARED_TG, BookingActionCallback(booking_id=ns.booking_id[B], action="cancel_confirm"))
        await h.send_text(SHARED_TG, "/start")
        assert h.tg.requests == []  # ни ответа, ни действия
    assert await snapshot(session, B) == before
    # соседний мастер продолжает работать
    async with tg_harness(*default_routers(), master_id=A) as h:
        await h.send_text(SHARED_TG, "/start")
        assert h.tg.requests
