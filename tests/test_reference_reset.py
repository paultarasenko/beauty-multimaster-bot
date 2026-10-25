"""«Сохранить как эталон» (у мастера №1) и «Сбросить к эталону» (у остальных): эталон хранится в БД, берётся у мастера №1 целиком
(данные, фото, фото профиля, аватарка бота), а мастер, бот, владелец и срок не меняются."""

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, GetFile, GetUserProfilePhotos, SendPhoto
from aiogram.types import Chat, File, Message, PhotoSize, Update, UserProfilePhotos
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update

import app.reference.service as reference
from app.config.settings import settings
from app.database.models import (
    BreakPeriod,
    Master,
    MasterBot,
    MasterProfile,
    MasterSettings,
    PortfolioItem,
    ReferenceSnapshot,
    ReferenceSnapshotPhoto,
    Review,
    Service,
    SuperadminAction,
    WorkingHours,
)
from app.database.session import make_session_factory
from app.multibot import management
from app.multibot.bot_avatar import BotPhotoError
from app.multibot.crypto import TokenCipher
from app.superadmin import service as sa
from app.superadmin.access import SuperadminAccessMiddleware
from app.superadmin.handlers import build_router as build_superadmin_router
from app.superadmin.ui import SaCallback, card_keyboard
from app.utils.master_settings import SETTING_FIELDS
from tests.conftest import TEST_DATABASE_URL
from tests.stage11_support import A, B, TOKEN, row_counts, snapshot, two_masters  # noqa: F401  (фикстура pytest)
from tests.tg import RecordingSession
from tests.webhook_utils import message_update  # noqa: F401

SUPERADMIN_ID = 424242
SOURCE, TARGET = A, B  # мастер №1 — эталон, мастер №2 получает его
# Что остаётся у мастера после «Сбросить к эталону»: данные эталона и служебные строки (бот). Записей, клиентов и т.п. нет.
TABLES_AFTER_APPLY = {
    "master_bots": 1,
    "master_profile": 1,
    "master_settings": 1,
    "services": 2,
    "reviews": 2,
    "portfolio_items": 2,
    "working_hours": 7,
    "breaks": 1,
    "master_quiet_mode": 1,  # личная настройка мастера: сброс к эталону её не трогает (отложенные сообщения стираются)
}


class PhotoSession(RecordingSession):
    """Telegram-заглушка. Файл «скачивается» как байты его адреса (у каждого файла свои); загрузка фото даёт file_id, который
    зависит от бота (как в настоящем Telegram); у бота может быть аватарка."""

    def __init__(self, prefix: str = "FID", *, avatar: bool = True) -> None:
        super().__init__()
        self.prefix = prefix
        self.has_avatar = avatar
        self.forbid_photo = False
        self.fail_get_file = False
        self.fail_avatar_read = False
        self.deleted: list[int] = []
        self.sent = 0
        self.downloaded: list[str] = []

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetFile):
            if self.fail_get_file:
                raise TelegramBadRequest(method=method, message="Bad Request: wrong file identifier/HTTP URL specified")
            return File(file_id=method.file_id, file_unique_id="f", file_path=f"photos/{method.file_id}.jpg")
        if isinstance(method, GetUserProfilePhotos):
            if self.fail_avatar_read:
                raise TelegramBadRequest(method=method, message="Bad Request: user not found")
            photos = [[PhotoSize(file_id="avatar-small", file_unique_id="a1", width=90, height=90),
                       PhotoSize(file_id="avatar-big", file_unique_id="a2", width=640, height=640)]] if self.has_avatar else []
            return UserProfilePhotos(total_count=len(photos), photos=photos)
        if isinstance(method, SendPhoto):
            self.requests.append(method)
            if self.forbid_photo:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot can't initiate conversation with a user")
            self.sent += 1
            return Message(
                message_id=1000 + self.sent,
                date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                photo=[
                    PhotoSize(file_id=f"{self.prefix}-small-{self.sent}", file_unique_id=f"u{self.sent}", width=90, height=90),
                    PhotoSize(file_id=f"{self.prefix}-{self.sent}", file_unique_id=f"v{self.sent}", width=800, height=800),
                ],
            )
        if isinstance(method, DeleteMessage):
            self.deleted.append(method.message_id)
        return await super().make_request(bot, method, timeout)

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        self.downloaded.append(url)
        yield b"jpeg:" + url.rsplit("/", 1)[-1].encode()


class AvatarCalls:
    """Подмена вызовов Bot API для аватарки (в тестах настоящих запросов нет)."""

    def __init__(self) -> None:
        self.set: list[tuple[str, bytes]] = []
        self.removed: list[str] = []
        self.error: str | None = None


@pytest.fixture(autouse=True)
def avatar_calls(monkeypatch):
    calls = AvatarCalls()

    async def fake_set(bot, photo):
        if calls.error:
            raise BotPhotoError(calls.error)
        calls.set.append((bot.token, photo))

    async def fake_remove(bot):
        if calls.error:
            raise BotPhotoError(calls.error)
        calls.removed.append(bot.token)

    monkeypatch.setattr(reference, "set_bot_avatar", fake_set)
    monkeypatch.setattr(reference, "remove_bot_avatar", fake_remove)
    return calls


@pytest_asyncio.fixture
async def factory(session, two_masters):
    f = make_session_factory(TEST_DATABASE_URL)
    # У мастера-эталона всё НЕ по умолчанию: услуга с копейками и буфером, настройки, тексты, фото профиля.
    async with f() as s:
        await s.execute(update(MasterProfile).where(MasterProfile.master_id == SOURCE).values(
            description="О мастере A", greeting_text="Привет от A", contact_username="anna_a", experience_years=7,
            map_url="https://maps.example/a", location_landmark="у ТЦ", specialization="маникюр", photo_file_id="profile-A-photo"))
        await s.execute(update(MasterSettings).where(MasterSettings.master_id == SOURCE).values(
            currency="USD", slot_interval_minutes=15, max_active_bookings=7, cancel_streak_limit=5, allow_reviews=False,
            reminder_2h_enabled=False, booking_confirmation_text="Запись {date} подтверждена"))
        await s.execute(update(Service).where(Service.id == two_masters.service_id[SOURCE]).values(price=Decimal("45.50"), buffer_minutes=10))
        await s.commit()
    yield f
    async with f() as s:
        await s.execute(delete(SuperadminAction))
        await s.execute(delete(ReferenceSnapshot))
        await s.commit()
    await f.kw["bind"].dispose()


async def _overwrite(factory, ns, tg, **kw):
    return await reference.overwrite_reference(
        factory, ns.cipher, actor_telegram_id=SUPERADMIN_ID, bot_factory=lambda token: Bot(token, session=tg), **kw
    )


async def _apply(factory, ns, master_id, tg, *, fsm_cleaner=None):
    async def no_fsm(_bot_id):
        return None

    return await reference.reset_master_to_reference(
        factory,
        ns.cipher,
        master_id=master_id,
        upload_chat_id=SUPERADMIN_ID,
        bot_factory=lambda token: Bot(token, session=tg),
        fsm_cleaner=fsm_cleaner or no_fsm,
    )


async def _content(session, master_id: int) -> dict:
    """Всё, что входит в эталон, в сравнимом виде (без id, привязки к мастеру и file_id)."""
    session.expire_all()
    profile = (await session.execute(select(MasterProfile).where(MasterProfile.master_id == master_id))).scalar_one()
    settings_row = (await session.execute(select(MasterSettings).where(MasterSettings.master_id == master_id))).scalar_one()
    services = (await session.execute(select(Service).where(Service.master_id == master_id).order_by(Service.sort_order, Service.id))).scalars().all()
    reviews = (await session.execute(select(Review).where(Review.master_id == master_id).order_by(Review.created_at, Review.id))).scalars().all()
    items = (await session.execute(select(PortfolioItem).where(PortfolioItem.master_id == master_id).order_by(PortfolioItem.sort_order, PortfolioItem.id))).scalars().all()
    hours = (await session.execute(select(WorkingHours).where(WorkingHours.master_id == master_id).order_by(WorkingHours.weekday))).scalars().all()
    breaks = (await session.execute(select(BreakPeriod).where(BreakPeriod.master_id == master_id).order_by(BreakPeriod.weekday, BreakPeriod.start_time, BreakPeriod.id))).scalars().all()
    return {
        "profile": {c.key: getattr(profile, c.key) for c in MasterProfile.__table__.columns if c.key not in {"id", "master_id", "created_at", "updated_at", "photo_file_id"}},
        "settings": {name: getattr(settings_row, name) for name in SETTING_FIELDS},
        "services": [(s.name, s.description, s.price, s.duration_minutes, s.buffer_minutes, s.active, s.sort_order) for s in services],
        "reviews": [(r.author_name, r.text, r.rating, r.active, r.created_at) for r in reviews],
        "portfolio": [(p.title, p.description, p.category, p.sort_order, p.active) for p in items],
        "hours": [(h.weekday, h.is_working, h.start_time, h.end_time) for h in hours],
        "breaks": [(b.weekday, b.start_time, b.end_time) for b in breaks],
    }


async def _snapshots(factory) -> list[ReferenceSnapshot]:
    async with factory() as s:
        return list((await s.execute(select(ReferenceSnapshot).order_by(ReferenceSnapshot.id))).scalars().all())


# ---- «Сохранить как эталон» ----


async def test_overwrite_stores_master_state_and_leaves_the_master_untouched(session, two_masters, factory):
    before = await snapshot(session, SOURCE)
    other_before = await snapshot(session, TARGET)

    result = await _overwrite(factory, two_masters, PhotoSession(avatar=True))

    session.expire_all()
    assert await snapshot(session, SOURCE) == before  # мастер №1 не изменился
    assert await snapshot(session, TARGET) == other_before
    assert (result.services, result.reviews, result.portfolio, result.profile_photo, result.avatar) == (2, 2, 2, True, reference.AVATAR_SET)
    [row] = await _snapshots(factory)
    assert row.source_master_id == SOURCE and row.created_by_telegram_id == SUPERADMIN_ID
    assert row.payload["profile"]["description"] == "О мастере A" and row.payload["settings"]["currency"] == "USD"
    assert [s["name"] for s in row.payload["services"]] == ["Услуга-A", "Скрытая-A"]  # порядок и скрытые услуги сохранены
    assert "telegram_file_id" not in row.payload["portfolio"][0] and "photo_file_id" not in row.payload["profile"]  # file_id в эталон не попадают


async def test_overwrite_stores_photo_files_not_file_ids(session, two_masters, factory):
    tg = PhotoSession()
    await _overwrite(factory, two_masters, tg)
    async with factory() as s:
        photos = {p.key: p.content for p in (await s.execute(select(ReferenceSnapshotPhoto))).scalars().all()}
    assert photos == {
        "portfolio:0": b"jpeg:file-A-1.jpg",
        "portfolio:1": b"jpeg:file-A-2.jpg",
        "profile": b"jpeg:profile-A-photo.jpg",
        "bot_avatar": b"jpeg:avatar-big.jpg",  # самый большой размер
    }


async def test_overwrite_without_avatar_records_that_the_bot_has_none(session, two_masters, factory):
    result = await _overwrite(factory, two_masters, PhotoSession(avatar=False))
    assert result.avatar == reference.AVATAR_NONE and result.avatar_warning is None
    async with factory() as s:
        keys = set((await s.execute(select(ReferenceSnapshotPhoto.key))).scalars().all())
    assert "bot_avatar" not in keys


async def test_overwrite_survives_unreadable_avatar_and_reports_it(session, two_masters, factory):
    tg = PhotoSession()
    tg.fail_avatar_read = True
    result = await _overwrite(factory, two_masters, tg)
    assert result.avatar == reference.AVATAR_UNKNOWN and "не скопировано" in result.avatar_warning
    assert TOKEN[SOURCE] not in result.avatar_warning
    assert len(await _snapshots(factory)) == 1  # остальной эталон сохранён


async def test_second_overwrite_replaces_the_first(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    async with factory() as s:
        await s.execute(update(MasterProfile).where(MasterProfile.master_id == SOURCE).values(name="Новое имя"))
        await s.commit()
    await _overwrite(factory, two_masters, PhotoSession())

    [row] = await _snapshots(factory)  # прежняя версия удалена вместе со своими фото
    assert row.payload["profile"]["name"] == "Новое имя"
    async with factory() as s:
        assert (await s.execute(select(func.count()).select_from(ReferenceSnapshotPhoto).where(ReferenceSnapshotPhoto.snapshot_id != row.id))).scalar_one() == 0


async def test_overwrite_download_failure_keeps_previous_reference(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    [first] = await _snapshots(factory)
    tg = PhotoSession()
    tg.fail_get_file = True
    with pytest.raises(reference.ReferenceResetError, match="Не удалось скачать фото через бота мастера №1") as exc:
        await _overwrite(factory, two_masters, tg)
    assert TOKEN[SOURCE] not in str(exc.value)
    assert [r.id for r in await _snapshots(factory)] == [first.id]


async def test_overwrite_requires_the_source_masters_bot(session, two_masters, factory):
    async with factory() as s:
        await s.execute(delete(MasterBot).where(MasterBot.master_id == SOURCE))
        await s.commit()
    with pytest.raises(reference.ReferenceResetError, match="нет бота"):
        await _overwrite(factory, two_masters, PhotoSession())
    assert await _snapshots(factory) == []


async def test_overwrite_does_not_depend_on_changes_after_it(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    async with factory() as s:  # мастер №1 продолжает менять своё: эталон — снимок на момент нажатия
        await s.execute(update(MasterProfile).where(MasterProfile.master_id == SOURCE).values(name="После снимка"))
        await s.execute(delete(Service).where(Service.master_id == SOURCE, Service.active.is_(False)))
        await s.commit()
    await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    content = await _content(session, TARGET)
    assert content["profile"]["name"] == "Мастер-A" and len(content["services"]) == 2


# ---- «Сбросить к эталону» ----


async def test_apply_copies_everything_from_the_source_master(session, two_masters, factory):
    ns = two_masters
    await _overwrite(factory, ns, PhotoSession())
    source_before = await snapshot(session, SOURCE)
    master_before = (await session.execute(select(Master.owner_telegram_id, Master.trial_ends_on, Master.is_active).where(Master.id == TARGET))).one()
    bot_before = (await snapshot(session, TARGET))["master_bots"]
    assert (await row_counts(session, TARGET))["bookings"] > 0  # до обновления у мастера есть записи и клиенты

    await _apply(factory, ns, TARGET, PhotoSession("B"))

    session.expire_all()
    assert await _content(session, TARGET) == await _content(session, SOURCE)  # данные мастера №2 = данные мастера №1
    assert {n: c for n, c in (await row_counts(session, TARGET)).items() if c} == TABLES_AFTER_APPLY  # записей, клиентов, заблокированного времени и т.п. нет
    assert await snapshot(session, SOURCE) == source_before  # источник не тронут
    assert (await snapshot(session, TARGET))["master_bots"] == bot_before  # токен, webhook и секрет те же
    master_after = (await session.execute(select(Master.owner_telegram_id, Master.trial_ends_on, Master.is_active).where(Master.id == TARGET))).one()
    assert master_after == master_before


async def test_apply_writes_exact_values(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    content = await _content(session, TARGET)

    assert content["settings"]["currency"] == "USD" and content["settings"]["slot_interval_minutes"] == 15
    assert content["settings"]["booking_confirmation_text"] == "Запись {date} подтверждена" and content["settings"]["allow_reviews"] is False
    assert content["profile"]["greeting_text"] == "Привет от A" and content["profile"]["timezone"] == "Europe/Riga"
    assert content["services"][0][2] == Decimal("45.50") and content["services"][0][4] == 10
    assert [s[0] for s in content["services"]] == ["Услуга-A", "Скрытая-A"] and content["services"][1][5] is False
    assert [(r[0], r[2]) for r in content["reviews"]] == [("Ревью-A-1", 5), ("Ревью-A-2", 4)]
    assert [(p[0], p[2]) for p in content["portfolio"]] == [("Работа-A-1", "cat-A"), ("Работа-A-2", "cat-A")]
    assert all(h[1] for h in content["hours"]) and len(content["hours"]) == 7
    reviews = (await session.execute(select(Review).where(Review.master_id == TARGET))).scalars().all()
    assert {r.client_id for r in reviews} == {None}  # привязка к клиентам мастера №1 не переносится


async def test_apply_replaces_target_lunch_with_the_reference_lunch(session, two_masters, factory):
    """Обед входит в расписание: у мастера №2 свой обед (13–14), у эталона другой (15–16) — после обновления остаётся эталонный."""
    target_before = (await session.execute(select(BreakPeriod.start_time, BreakPeriod.end_time).where(BreakPeriod.master_id == TARGET))).all()
    assert target_before == [(dt.time(13), dt.time(14))]

    await _overwrite(factory, two_masters, PhotoSession())
    await _apply(factory, two_masters, TARGET, PhotoSession("B"))

    session.expire_all()
    rows = (await session.execute(select(BreakPeriod.weekday, BreakPeriod.start_time, BreakPeriod.end_time).where(BreakPeriod.master_id == TARGET))).all()
    assert rows == [(0, dt.time(15), dt.time(16))]


async def test_apply_of_reference_without_breaks_key_still_works(session, two_masters, factory):
    """Эталон, снятый до того, как в снимок стали попадать обеды, не содержит ключа breaks: применение не падает и обедов не создаёт."""
    await _overwrite(factory, two_masters, PhotoSession())
    async with factory() as s:
        snapshot_row = (await s.execute(select(ReferenceSnapshot))).scalar_one()
        snapshot_row.payload = {k: v for k, v in snapshot_row.payload.items() if k != "breaks"}
        await s.commit()

    await _apply(factory, two_masters, TARGET, PhotoSession("B"))

    session.expire_all()
    assert (await session.execute(select(func.count()).select_from(BreakPeriod).where(BreakPeriod.master_id == TARGET))).scalar_one() == 0


async def test_apply_uploads_photos_through_target_bot_and_deletes_messages(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    tg = PhotoSession("B")
    await _apply(factory, two_masters, TARGET, tg)

    sent = [m for m in tg.requests if isinstance(m, SendPhoto)]
    assert [m.photo.data for m in sent] == [b"jpeg:file-A-1.jpg", b"jpeg:file-A-2.jpg", b"jpeg:profile-A-photo.jpg"]
    assert {m.chat_id for m in sent} == {SUPERADMIN_ID} and all(m.disable_notification for m in sent)
    assert sorted(tg.deleted) == [1001, 1002, 1003]  # у суперадмина не остаётся служебных фото
    session.expire_all()
    items = (await session.execute(select(PortfolioItem).where(PortfolioItem.master_id == TARGET).order_by(PortfolioItem.sort_order))).scalars().all()
    assert [p.telegram_file_id for p in items] == ["B-1", "B-2"]  # file_id именно бота мастера №2
    profile = (await session.execute(select(MasterProfile).where(MasterProfile.master_id == TARGET))).scalar_one()
    assert profile.photo_file_id == "B-3"


async def test_apply_sets_bot_avatar_after_data_replaced(session, two_masters, factory, avatar_calls):
    await _overwrite(factory, two_masters, PhotoSession())
    result = await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    assert avatar_calls.set == [(TOKEN[TARGET], b"jpeg:avatar-big.jpg")] and avatar_calls.removed == []
    assert result.avatar_warning is None


async def test_apply_removes_avatar_when_reference_bot_has_none(session, two_masters, factory, avatar_calls):
    await _overwrite(factory, two_masters, PhotoSession(avatar=False))
    await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    assert avatar_calls.removed == [TOKEN[TARGET]] and avatar_calls.set == []


async def test_apply_leaves_avatar_alone_when_reference_avatar_is_unknown(session, two_masters, factory, avatar_calls):
    tg = PhotoSession()
    tg.fail_avatar_read = True
    await _overwrite(factory, two_masters, tg)
    await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    assert avatar_calls.set == [] and avatar_calls.removed == []


async def test_avatar_failure_is_a_warning_and_data_is_still_replaced(session, two_masters, factory, avatar_calls):
    await _overwrite(factory, two_masters, PhotoSession())
    avatar_calls.error = "Bad Request: PHOTO_INVALID"
    result = await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    assert "PHOTO_INVALID" in result.avatar_warning
    assert await _content(session, TARGET) == await _content(session, SOURCE)


async def test_apply_without_reference_is_refused_and_changes_nothing(session, two_masters, factory):
    before = await snapshot(session, TARGET)
    with pytest.raises(reference.ReferenceResetError, match="Эталон ещё не создан.*Сохранить как эталон"):
        await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    session.expire_all()
    assert await snapshot(session, TARGET) == before


async def test_apply_to_the_source_master_is_refused(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    before = await snapshot(session, SOURCE)
    with pytest.raises(reference.ReferenceResetError, match="источник эталона"):
        await _apply(factory, two_masters, SOURCE, PhotoSession())
    session.expire_all()
    assert await snapshot(session, SOURCE) == before


async def test_apply_is_repeatable_and_removes_tester_changes(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    async with factory() as s:  # тестер правит данные
        await s.execute(update(MasterProfile).where(MasterProfile.master_id == TARGET).values(name="Тестер", address="другой"))
        await s.execute(update(MasterSettings).where(MasterSettings.master_id == TARGET).values(currency="RUB"))
        await s.commit()
    await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    assert await _content(session, TARGET) == await _content(session, SOURCE)
    assert {n: c for n, c in (await row_counts(session, TARGET)).items() if c} == TABLES_AFTER_APPLY


async def test_apply_upload_failure_changes_nothing_and_asks_to_press_start(session, two_masters, factory, avatar_calls):
    await _overwrite(factory, two_masters, PhotoSession())
    before = await snapshot(session, TARGET)
    tg = PhotoSession("B")
    tg.forbid_photo = True
    with pytest.raises(reference.ReferenceResetError, match="/start") as exc:
        await _apply(factory, two_masters, TARGET, tg)
    assert TOKEN[TARGET] not in str(exc.value)
    session.expire_all()
    assert await snapshot(session, TARGET) == before
    assert avatar_calls.set == [] and avatar_calls.removed == []  # аватарку не трогаем, пока база не заменена


async def test_apply_without_target_bot_is_refused(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    async with factory() as s:
        await s.execute(delete(MasterBot).where(MasterBot.master_id == TARGET))
        await s.commit()
    with pytest.raises(reference.ReferenceResetError, match="нет бота"):
        await _apply(factory, two_masters, TARGET, PhotoSession("B"))


async def test_apply_reports_damaged_reference(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    async with factory() as s:
        await s.execute(delete(ReferenceSnapshotPhoto).where(ReferenceSnapshotPhoto.key == "portfolio:1"))
        await s.commit()
    before = await snapshot(session, TARGET)
    with pytest.raises(reference.ReferenceResetError, match="повреждён"):
        await _apply(factory, two_masters, TARGET, PhotoSession("B"))
    session.expire_all()
    assert await snapshot(session, TARGET) == before


async def test_apply_clears_fsm_states_of_the_masters_bot(session, two_masters, factory):
    await _overwrite(factory, two_masters, PhotoSession())
    cleaned: list[int] = []

    async def cleaner(bot_id):
        cleaned.append(bot_id)

    await _apply(factory, two_masters, TARGET, PhotoSession("B"), fsm_cleaner=cleaner)
    assert cleaned == [(await snapshot(session, TARGET))["master_bots"][0][2]]  # telegram_bot_id мастера-получателя


def test_columns_bound_to_a_bot_are_never_copied_as_values():
    for name, (model, skip) in reference._SPECS.items():
        copied = {c.key for c in model.__table__.columns if c.key not in skip}
        assert not {c for c in copied if "file_id" in c}, f"{name}: file_id нельзя копировать между ботами"
        assert not {"id", "master_id"} & copied


# ---- кнопки и сценарии суперадмин-бота ----


def _callbacks(card) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in card_keyboard(card).inline_keyboard]


async def test_source_master_card_has_overwrite_button_instead_of_update(session, two_masters, factory):
    rows = _callbacks(await sa.get_master_card(factory, SOURCE))
    flat = [c for row in rows for c in row]
    assert SaCallback(action="ask_ow", master_id=SOURCE).pack() in flat
    assert SaCallback(action="ask_rs", master_id=SOURCE).pack() not in flat
    disable_row = rows.index([SaCallback(action="ask_md", master_id=SOURCE).pack()])
    assert rows[disable_row + 1] == [SaCallback(action="ask_ow", master_id=SOURCE).pack()]  # на месте «Сбросить к эталону»


async def test_other_master_card_keeps_update_button(session, two_masters, factory):
    rows = _callbacks(await sa.get_master_card(factory, TARGET))
    flat = [c for row in rows for c in row]
    assert SaCallback(action="ask_rs", master_id=TARGET).pack() in flat
    assert SaCallback(action="ask_ow", master_id=TARGET).pack() not in flat
    disable_row = rows.index([SaCallback(action="ask_md", master_id=TARGET).pack()])
    assert rows[disable_row + 1] == [SaCallback(action="ask_rs", master_id=TARGET).pack()]
    assert rows[disable_row + 2] == [SaCallback(action="mj", master_id=TARGET).pack(), SaCallback(action="more", master_id=TARGET).pack()]
    assert rows[disable_row + 3] == [SaCallback(action="list", master_id=0).pack()]


async def test_buttons_absent_without_a_bot(session, two_masters, factory):
    async with factory() as s:
        await s.execute(delete(MasterBot).where(MasterBot.master_id.in_([SOURCE, TARGET])))
        await s.commit()
    for master_id in (SOURCE, TARGET):
        flat = [c for row in _callbacks(await sa.get_master_card(factory, master_id)) for c in row]
        assert not [c for c in flat if c.startswith(("sa:ask_rs", "sa:ask_ow"))]


def _callback_update(update_id: int, data_: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": SUPERADMIN_ID, "is_bot": False, "first_name": "sa"},
            "chat_instance": "x",
            "data": data_,
            "message": {"message_id": 1, "date": int(dt.datetime.now(dt.timezone.utc).timestamp()), "chat": {"id": SUPERADMIN_ID, "type": "private"}, "text": "card"},
        },
    }


@pytest_asyncio.fixture
async def sa_bot(session, two_masters, factory, monkeypatch):
    """Настоящий диспетчер суперадмин-бота; Telegram мастеров — заглушки, токены зашифрованы ключом из настроек."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "bot_token_encryption_key", key)
    async with factory() as s:
        for master_id in (SOURCE, TARGET):
            await s.execute(update(MasterBot).where(MasterBot.master_id == master_id).values(bot_token_encrypted=TokenCipher(key).encrypt(TOKEN[master_id])))
        await s.commit()
    masters_tg = {TOKEN[SOURCE]: PhotoSession("A"), TOKEN[TARGET]: PhotoSession("B")}
    monkeypatch.setattr(management, "default_bot_factory", lambda token: Bot(token, session=masters_tg[token]))

    async def no_fsm(_bot_id):
        return None

    monkeypatch.setattr(management, "default_fsm_cleaner", no_fsm)

    tg = RecordingSession()
    bot = Bot("777000:" + "S" * 35, session=tg)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware.register(SuperadminAccessMiddleware(frozenset({SUPERADMIN_ID})))
    dp.include_router(build_superadmin_router())
    dp.workflow_data.update(session_factory=factory, registry=None, health_monitor=None, sa_bot_id=777000)

    counter = iter(range(1, 1000))

    async def press(action: str, master_id: int) -> None:
        raw = _callback_update(next(counter), SaCallback(action=action, master_id=master_id).pack())
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    return type("SaBot", (), {"press": staticmethod(press), "tg": tg, "masters_tg": masters_tg})


async def test_button_flow_overwrite_then_update(session, two_masters, factory, sa_bot):
    source_before = await snapshot(session, SOURCE)

    await sa_bot.press("ask_ow", SOURCE)
    assert any("Сохранить мастера №1 как эталон?" in t and "Мастер №1 при этом не меняется" in t for t in sa_bot.tg.texts())
    assert await _snapshots(factory) == [] and sa_bot.masters_tg[TOKEN[SOURCE]].sent == 0  # пока только вопрос

    await sa_bot.press("do_ow", SOURCE)
    assert len(await _snapshots(factory)) == 1
    assert any("Эталон сохранён" in t for t in sa_bot.tg.texts())
    session.expire_all()
    assert await snapshot(session, SOURCE) == source_before

    await sa_bot.press("ask_rs", TARGET)
    assert any("Сбросить мастера №2 к эталону?" in t and "без возможности восстановления" in t for t in sa_bot.tg.texts())
    assert (await row_counts(session, TARGET))["bookings"] > 0

    await sa_bot.press("do_rs", TARGET)
    session.expire_all()
    assert await _content(session, TARGET) == await _content(session, SOURCE)
    assert any("Мастер сброшен к эталону" in t for t in sa_bot.tg.texts())
    actions = {(a.action, a.target_master_id, a.result) for a in await sa.recent_actions(factory)}
    assert {("overwrite_reference", SOURCE, "ok"), ("reset_reference", TARGET, "ok")} <= actions


async def test_overwrite_button_is_refused_for_other_masters(session, two_masters, factory, sa_bot):
    await sa_bot.press("do_ow", TARGET)
    assert await _snapshots(factory) == []
    assert not [a for a in await sa.recent_actions(factory) if a.action == "overwrite_reference"]


async def test_update_button_reports_missing_reference(session, two_masters, factory, sa_bot):
    before = await snapshot(session, TARGET)
    await sa_bot.press("do_rs", TARGET)
    session.expire_all()
    assert await snapshot(session, TARGET) == before
    assert any("Сброс не выполнен" in t and "Эталон ещё не создан" in t for t in sa_bot.tg.texts())
    assert any(a.action == "reset_reference" and a.result != "ok" for a in await sa.recent_actions(factory))


async def test_overwrite_failure_keeps_previous_reference_and_reports(session, two_masters, factory, sa_bot):
    await sa_bot.press("do_ow", SOURCE)
    [first] = await _snapshots(factory)
    sa_bot.masters_tg[TOKEN[SOURCE]].fail_get_file = True
    await sa_bot.press("do_ow", SOURCE)
    assert [r.id for r in await _snapshots(factory)] == [first.id]
    assert any("Эталон не сохранён, прежний остался" in t for t in sa_bot.tg.texts())
    assert any(a.action == "overwrite_reference" and a.result != "ok" for a in await sa.recent_actions(factory))
