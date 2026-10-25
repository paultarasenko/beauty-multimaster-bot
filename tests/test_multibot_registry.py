"""BotRegistry: кэш, добавление/удаление/обновление без перезапуска, закрытие сессий."""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

from app.database.models import Master, MasterBot
from app.database.session import make_session_factory
from app.multibot.crypto import TokenCipher, generate_webhook_secret
from app.multibot.registry import BotRegistry
from tests.conftest import TEST_DATABASE_URL
from tests.webhook_utils import FakeTelegram, token_for

BOT_A, BOT_B = 1001, 1002


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key().decode())


async def add_row(session, cipher, master_id: int, bot_id: int, *, active: bool = True, token: str | None = None) -> MasterBot:
    row = MasterBot(
        master_id=master_id,
        telegram_bot_id=bot_id,
        bot_username=f"bot{bot_id}_bot",
        bot_token_encrypted=cipher.encrypt(token or token_for(bot_id)),
        webhook_secret=generate_webhook_secret(),
        is_active=active,
    )
    session.add(row)
    await session.commit()
    return row


async def add_master(session, master_id: int, owner: int) -> None:
    session.add(Master(id=master_id, owner_telegram_id=owner))
    await session.commit()


def make_registry(cipher, tg=None, **kwargs) -> tuple[BotRegistry, FakeTelegram]:
    tg = tg or FakeTelegram()
    return BotRegistry(make_session_factory(TEST_DATABASE_URL), cipher, http_session_factory=lambda: tg, **kwargs), tg


async def test_load_all_creates_and_caches_bots_with_shared_session(session, cipher):
    await add_master(session, 2, 777)
    await add_row(session, cipher, 1, BOT_A)
    await add_row(session, cipher, 2, BOT_B)
    registry, tg = make_registry(cipher)

    assert await registry.load_all() == 2
    a, b = await registry.get(BOT_A), await registry.get(BOT_B)
    assert (a.master_id, b.master_id) == (1, 2)
    assert a.bot.token == token_for(BOT_A) and a.bot is not b.bot
    assert a.bot.session is b.bot.session is tg  # одна общая HTTP-сессия
    assert await registry.get(BOT_A) is a  # из кэша: тот же объект
    assert registry.master_id_for_bot(a.bot) == 1 and registry.master_id_for_bot(b.bot) == 2
    assert {e.master_id for e in registry.active_entries()} == {1, 2}
    await registry.close()


async def test_entry_repr_does_not_contain_token_or_secrets(session, cipher):
    row = await add_row(session, cipher, 1, BOT_A)
    registry, _ = make_registry(cipher)
    await registry.load_all()
    entry = await registry.get(BOT_A)
    text = repr(entry)
    assert token_for(BOT_A) not in text and row.webhook_secret not in text and entry.header_token not in text
    await registry.close()


async def test_bot_added_by_another_process_is_found_lazily(session, cipher):
    registry, _ = make_registry(cipher, negative_ttl=0)
    await registry.load_all()
    assert await registry.get(BOT_A) is None
    await add_row(session, cipher, 1, BOT_A)  # «CLI в другом процессе»
    entry = await registry.get(BOT_A)
    assert entry is not None and entry.accepts_updates
    await registry.close()


async def test_unknown_bot_lookup_is_negatively_cached(session, cipher):
    registry, _ = make_registry(cipher, negative_ttl=60)
    assert await registry.get(BOT_A) is None
    await add_row(session, cipher, 1, BOT_A)
    assert await registry.get(BOT_A) is None  # «нет» запомнено — БД не дёргается снова
    await registry.refresh(BOT_A)
    assert await registry.get(BOT_A) is not None
    await registry.close()


async def test_stale_entry_is_reread_and_picks_up_deactivation_and_removal(session, cipher):
    row = await add_row(session, cipher, 1, BOT_A)
    registry, _ = make_registry(cipher, ttl=0)  # запись сразу считается протухшей
    await registry.load_all()
    assert (await registry.get(BOT_A)).accepts_updates

    await session.execute(update(MasterBot).where(MasterBot.id == row.id).values(is_active=False))
    await session.commit()
    entry = await registry.get(BOT_A)
    assert entry.is_active is False and entry.bot is None and not entry.accepts_updates

    await session.delete(await session.get(MasterBot, row.id))
    await session.commit()
    assert await registry.get(BOT_A) is None
    await registry.close()


async def test_inactive_master_is_not_accepting_updates(session, cipher):
    await add_master(session, 2, 777)
    await add_row(session, cipher, 2, BOT_B)
    await session.execute(update(Master).where(Master.id == 2).values(is_active=False))
    await session.commit()
    registry, _ = make_registry(cipher)
    await registry.load_all()
    assert (await registry.get(BOT_B)).accepts_updates is False
    assert registry.active_entries() == []
    await registry.close()


async def test_put_remove_and_refresh_without_restart(session, cipher):
    registry, _ = make_registry(cipher, ttl=3600)
    row = await add_row(session, cipher, 1, BOT_A)
    entry = registry.put(row)
    assert (await registry.get(BOT_A)) is entry
    assert registry.remove(BOT_A) is True and registry.remove(BOT_A) is False
    assert len(registry) == 0

    # refresh с тем же токеном сохраняет тот же объект Bot, с новым — создаёт другой
    first = await registry.refresh(BOT_A)
    same = await registry.refresh(BOT_A)
    assert same.bot is first.bot
    new_token = f"{BOT_A}:" + "B" * 35
    await session.execute(update(MasterBot).where(MasterBot.id == row.id).values(bot_token_encrypted=cipher.encrypt(new_token)))
    await session.commit()
    changed = await registry.refresh(BOT_A)
    assert changed.bot is not first.bot and changed.bot.token == new_token
    await registry.close()


async def test_undecryptable_token_disables_bot_without_crash(session, cipher):
    await add_row(session, cipher, 1, BOT_A)
    other_cipher = TokenCipher(Fernet.generate_key().decode())
    registry, _ = make_registry(other_cipher)
    await registry.load_all()
    entry = await registry.get(BOT_A)
    assert entry.bot is None and not entry.accepts_updates
    await registry.close()


async def test_foreign_bot_object_is_not_mapped_to_a_master(session, cipher):
    from aiogram import Bot

    await add_row(session, cipher, 1, BOT_A)
    registry, _ = make_registry(cipher)
    await registry.load_all()
    assert registry.master_id_for_bot(Bot(token=token_for(BOT_A))) is None  # тот же id, но не созданный реестром
    await registry.close()


async def test_close_closes_shared_session_once_and_clears_cache(session, cipher):
    await add_row(session, cipher, 1, BOT_A)
    registry, tg = make_registry(cipher)
    await registry.load_all()
    registry.remove(BOT_A)
    assert tg.closed is False  # удаление одного бота общую сессию не закрывает
    await registry.load_all()
    await registry.close()
    assert tg.closed is True and len(registry) == 0
    await registry.close()  # повторный вызов безопасен
