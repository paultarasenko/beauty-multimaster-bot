"""Генератор нагрузки: апдейты Telegram на webhook приложения (/tg/{bot_id}/{secret}) от имени клиентов разных мастеров.

Открытая модель: новые «сессии» клиентов приходят по расписанию (пуассоновский поток с заданной средней скоростью) и НЕ ждут,
пока приложение ответит прежним. Шаги внутри одной сессии идут друг за другом (клиент не нажмёт «Время», не получив даты) с паузой
на «раздумье». Время ответа считается от ЗАПЛАНИРОВАННОГО момента отправки (а не от фактического): так задержка самого
генератора не прячет медленные ответы приложения. Отставание генератора измеряется отдельно (lag_ms) и попадает в отчёт.

Сессии (доли — SESSION_WEIGHTS): «меню» (/start, услуги, информация…), «запись» (услуга → дата → время → подтверждение, затем
в 70 % случаев отмена: «Мои записи» → «Отменить» → «Да, отменить»), «просмотр» (до выбора времени и уход).
Кнопки берутся из ответов бота (поддельный Telegram хранит последнюю клавиатуру) и разбираются классами CallbackData приложения.
"""

import asyncio
import itertools
import json
import logging
import math
import random
from collections import Counter
from dataclasses import dataclass, field

import aiohttp
import asyncpg

from app.multibot.crypto import TokenCipher
from tools.loadtest.common import TEST_OWNER_MIN
from tools.loadtest.updates import (
    BOOK_TEXT,
    MENU_TEXTS,
    MY_BOOKINGS_TEXT,
    BookingActionCallback,
    ConfirmCallback,
    DateCallback,
    ServiceCallback,
    TimeCallback,
    callback_update,
    message_update,
    pick_callback,
)

logger = logging.getLogger("loadtest")

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
SESSION_KINDS = ("menu", "book", "browse")
SESSION_WEIGHTS = (0.3, 0.5, 0.2)
CANCEL_PROBABILITY = 0.7
# Среднее число апдейтов в сессии при этих долях (меню ≈ 2, запись ≈ 5 + 0.7 × 3, просмотр = 4): по нему скорость апдейтов
# переводится в скорость появления сессий. Фактическая скорость измеряется и пишется в отчёт рядом с заданной.
MEAN_STEPS_PER_SESSION = 5.0
CLIENTS_PER_MASTER = 200


@dataclass
class Tenant:
    """Мастер со своим ботом и пулом клиентов; один клиент одновременно участвует только в одной сессии."""

    master_id: int
    bot_id: int
    owner_telegram_id: int
    secret: str = field(repr=False)
    header_token: str = field(repr=False)
    clients: list[int] = field(repr=False)
    busy: set[int] = field(default_factory=set, repr=False)

    def acquire(self, rng: random.Random) -> int | None:
        for _ in range(12):
            candidate = rng.choice(self.clients)
            if candidate not in self.busy:
                return candidate
        return None


@dataclass(slots=True)
class Sample:
    t: float  # секунды от начала прогона до запланированной отправки
    latency_ms: float  # от запланированной отправки до конца ответа
    lag_ms: float  # насколько генератор опоздал с отправкой
    status: int  # HTTP-статус; 0 — ошибка соединения или таймаут
    master_id: int
    step: str
    phase: str
    group: str
    error: str = ""


@dataclass
class PhaseResult:
    name: str
    target_rate: float
    duration: float
    sessions_started: int = 0
    sessions_skipped: int = 0  # не нашлось свободного клиента мастера (пул занят)
    t_start: float = 0.0
    t_end: float = 0.0


class FakeClient:
    """Управление поддельным Telegram по HTTP."""

    def __init__(self, http: aiohttp.ClientSession, base_url: str):
        self._http, self._base = http, base_url.rstrip("/")

    async def _json(self, method: str, path: str, **kwargs):
        async with self._http.request(method, f"{self._base}{path}", **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    async def stats(self) -> dict:
        return await self._json("GET", "/__stats")

    async def reset(self, *, config: bool = False) -> None:
        await self._json("POST", "/__reset", params={"config": "1"} if config else None)

    async def configure(self, **patch) -> dict:
        return await self._json("POST", "/__config", json=patch)

    async def last(self, bot_id: int, chat_id: int) -> dict | None:
        return await self._json("GET", "/__last", params={"bot_id": bot_id, "chat_id": chat_id})


async def load_tenants(dsn: str, encryption_key: str) -> list[Tenant]:
    """Тестовые мастера с активными ботами, их клиенты и заголовок секрета webhook (как его проверяет приложение)."""
    cipher = TokenCipher(encryption_key)
    conn = await asyncpg.connect(dsn)
    try:
        masters = await conn.fetch(
            """SELECT m.id, m.owner_telegram_id, b.telegram_bot_id, b.webhook_secret
               FROM masters m JOIN master_bots b ON b.master_id = m.id
               WHERE m.owner_telegram_id >= $1 AND m.is_active AND b.is_active ORDER BY m.id""",
            TEST_OWNER_MIN,
        )
        clients: dict[int, list[int]] = {}
        for row in await conn.fetch(
            """SELECT mc.master_id, u.telegram_id FROM master_clients mc JOIN users u ON u.id = mc.user_id
               WHERE u.telegram_id >= $1 ORDER BY mc.master_id, u.telegram_id""",
            TEST_OWNER_MIN,
        ):
            clients.setdefault(row["master_id"], []).append(row["telegram_id"])
    finally:
        await conn.close()
    return [
        Tenant(
            master_id=row["id"], bot_id=row["telegram_bot_id"], owner_telegram_id=row["owner_telegram_id"], secret=row["webhook_secret"],
            header_token=cipher.header_token(row["webhook_secret"]), clients=clients[row["id"]][:CLIENTS_PER_MASTER],
        )
        for row in masters
        if clients.get(row["id"])
    ]


async def ensure_bots_active(dsn: str) -> int:
    """Боты тестовых мастеров, которых планировщик мог отключить (ответ 401 в прошлом прогоне), включаются обратно. Возвращает число."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """UPDATE master_bots SET is_active = true WHERE NOT is_active
               AND master_id IN (SELECT id FROM masters WHERE owner_telegram_id >= $1) RETURNING id""",
            TEST_OWNER_MIN,
        )
    finally:
        await conn.close()
    return len(rows)


class Dialog:
    """Один клиент в одной сессии: отправка шагов и чтение ответов бота."""

    def __init__(self, gen: "Generator", tenant: Tenant, telegram_id: int, intended: float, phase: str, group: str, record: bool):
        self.gen, self.tenant, self.tg = gen, tenant, telegram_id
        self.intended, self.phase, self.group, self.record = intended, phase, group, record
        self.steps = 0
        self.last_end = intended
        self.seen_seq = 0
        self.message_id = 1

    async def sync(self) -> None:
        """Запоминает, какие ответы бота в этом чате уже были (чужие старые клавиатуры не принимаем за свежие)."""
        last = await self.gen.fake.last(self.tenant.bot_id, self.tg)
        self.seen_seq = last["seq"] if last else 0

    async def markup(self) -> dict | None:
        """Клавиатура нового ответа бота после последнего шага; None — бот ничего нового с кнопками не прислал."""
        last = await self.gen.fake.last(self.tenant.bot_id, self.tg)
        if not last or last["seq"] <= self.seen_seq:
            return None
        self.seen_seq = last["seq"]
        self.message_id = last["message_id"]
        return last["reply_markup"]

    async def _think(self) -> None:
        delay = self.gen.rng.uniform(*self.gen.think)
        self.intended = self.last_end + delay
        pause = self.intended - asyncio.get_running_loop().time()
        if pause > 0:
            await asyncio.sleep(pause)

    async def say(self, step: str, text: str) -> bool:
        update = message_update(self.gen.next_id(), self.tg, text, self.message_id + 1)
        return await self._post(step, update)

    async def press(self, step: str, callback) -> bool:
        update = callback_update(self.gen.next_id(), self.tg, callback.pack(), self.message_id, str(self.gen.next_id()))
        return await self._post(step, update)

    async def _post(self, step: str, update: dict) -> bool:
        if self.steps:
            await self._think()
        self.steps += 1
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, error = 0, ""
        try:
            async with self.gen.http.post(
                f"{self.gen.app_url}/tg/{self.tenant.bot_id}/{self.tenant.secret}",
                data=json.dumps(update),
                headers={"Content-Type": "application/json", SECRET_HEADER: self.tenant.header_token},
                timeout=aiohttp.ClientTimeout(total=self.gen.timeout),
            ) as response:
                await response.read()
                status = response.status
        except Exception as exc:  # таймаут, обрыв соединения
            error = type(exc).__name__
        finished = loop.time()
        self.last_end = finished
        if self.record:
            self.gen.samples.append(
                Sample(
                    t=self.intended - self.gen.t_origin, latency_ms=(finished - self.intended) * 1000, lag_ms=max(0.0, (started - self.intended) * 1000),
                    status=status, master_id=self.tenant.master_id, step=step, phase=self.phase, group=self.group, error=error,
                )
            )
        return status == 200


class Generator:
    def __init__(
        self, tenants: list[Tenant], http: aiohttp.ClientSession, fake: FakeClient, app_url: str, *,
        rng: random.Random, think: tuple[float, float] = (0.3, 1.5), timeout: float = 30.0,
    ):
        self.tenants, self.http, self.fake, self.app_url = tenants, http, fake, app_url.rstrip("/")
        self.rng, self.think, self.timeout = rng, think, timeout
        self.samples: list[Sample] = []
        self.notes: Counter = Counter()  # «бот не прислал кнопки», «лимит записей» и т.п.: сколько сессий оборвалось и почему
        self.internal_errors = 0
        self.t_origin = asyncio.get_running_loop().time()
        self._ids = itertools.count(1_000_000)

    def next_id(self) -> int:
        return next(self._ids)

    # ---- сессии ----

    async def _kind_menu(self, d: Dialog) -> None:
        if self.rng.random() < 0.5 and not await d.say("start", "/start"):
            return
        for text in self.rng.sample(MENU_TEXTS[:4], k=self.rng.randint(1, 2)):
            if not await d.say("menu", text):
                return

    async def _book(self, d: Dialog, *, confirm: bool) -> bool:
        """Услуга → дата → время (→ подтверждение). Каждую кнопку выбирает из ответа бота; True — запись создана."""
        if not await d.say("book_start", BOOK_TEXT):
            return False
        for step, cls in (("book_service", ServiceCallback), ("book_date", DateCallback), ("book_time", TimeCallback)):
            callback = pick_callback(await d.markup(), cls, self.rng)
            if callback is None:
                self.notes[f"нет кнопок {cls.__prefix__} (лимит записей, нет дат или бот не ответил)"] += 1
                return False
            if not await d.press(step, callback):
                return False
        if not confirm:
            return False
        return await d.press("book_confirm", ConfirmCallback(action="yes"))

    async def _cancel_last(self, d: Dialog) -> None:
        if not await d.say("my_bookings", MY_BOOKINGS_TEXT):
            return
        for step, action in (("cancel_ask", "cancel"), ("cancel_confirm", "cancel_confirm")):
            callback = pick_callback(await d.markup(), BookingActionCallback, self.rng, lambda cb, a=action: cb.action == a)
            if callback is None:
                self.notes[f"нет кнопки отмены ({action})"] += 1
                return
            if not await d.press(step, callback):
                return

    async def _kind_book(self, d: Dialog) -> None:
        if await self._book(d, confirm=True) and self.rng.random() < CANCEL_PROBABILITY:
            await self._cancel_last(d)

    async def _kind_browse(self, d: Dialog) -> None:
        await self._book(d, confirm=False)

    async def _session(self, tenant: Tenant, intended: float, phase: str, group: str, record: bool, result: PhaseResult) -> None:
        telegram_id = tenant.acquire(self.rng)
        if telegram_id is None:
            result.sessions_skipped += 1
            return
        tenant.busy.add(telegram_id)
        try:
            dialog = Dialog(self, tenant, telegram_id, intended, phase, group, record)
            await dialog.sync()
            kind = self.rng.choices(SESSION_KINDS, SESSION_WEIGHTS)[0]
            await getattr(self, f"_kind_{kind}")(dialog)
        except Exception:
            self.internal_errors += 1
            logger.exception("внутренняя ошибка сценария клиента (master_id=%s)", tenant.master_id)
        finally:
            tenant.busy.discard(telegram_id)

    # ---- фаза нагрузки ----

    async def run_phase(
        self, name: str, rate: float, duration: float, *, weights: list[float] | None = None, special_master: int | None = None,
        record: bool = True,
    ) -> PhaseResult:
        """Идёт `duration` секунд со скоростью `rate` апдейтов/с (в среднем); сессии, начатые до конца, доигрываются."""
        loop = asyncio.get_running_loop()
        result = PhaseResult(name, rate, duration, t_start=loop.time() - self.t_origin)
        session_rate = rate / MEAN_STEPS_PER_SESSION
        start = loop.time()
        end, next_at = start + duration, start
        running: set[asyncio.Task] = set()
        while True:
            next_at += self.rng.expovariate(session_rate)
            if next_at >= end:
                break
            pause = next_at - loop.time()
            if pause > 0:
                await asyncio.sleep(pause)
            tenant = self.rng.choices(self.tenants, weights=weights)[0]
            group = "all" if special_master is None else ("special" if tenant.master_id == special_master else "others")
            task = asyncio.create_task(self._session(tenant, next_at, name, group, record, result))
            result.sessions_started += 1
            running.add(task)
            task.add_done_callback(running.discard)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        result.t_end = loop.time() - self.t_origin
        return result


def share_weights(tenants: list[Tenant], special_master: int, share: float) -> list[float]:
    """Вес мастера `special_master` = share, остальные делят 1 - share поровну."""
    others = len(tenants) - 1
    return [share if t.master_id == special_master else ((1 - share) / others if others else 0.0) for t in tenants]


async def run_load_profile(
    gen: Generator, fake: FakeClient, profile: str, *, rate: float, duration: float, warmup: float, baseline_duration: float,
    hot_share: float, bad_share: float, bad_mode: str, bad_delay_ms: float,
) -> list[PhaseResult]:
    """normal / peak — одна фаза; storm — базовая фаза и фаза с «горячим» мастером; bad_bot — базовая фаза и фаза с плохим ботом."""
    phases: list[PhaseResult] = []
    if warmup > 0:  # прогрев: реестр ботов, пул соединений, FSM; в статистику не входит
        await gen.run_phase("warmup", min(rate, 10.0), warmup, record=False)
    if profile in ("normal", "peak"):
        phases.append(await gen.run_phase("main", rate, duration))
    elif profile == "storm":
        hot = gen.tenants[0].master_id
        phases.append(await gen.run_phase("baseline", rate, baseline_duration))
        phases.append(await gen.run_phase("storm", rate, duration, weights=share_weights(gen.tenants, hot, hot_share), special_master=hot))
    elif profile == "bad_bot":
        bad = gen.tenants[0]
        phases.append(await gen.run_phase("baseline", rate, baseline_duration))
        if bad_mode == "slow":
            await fake.configure(slow_bots={str(bad.bot_id): bad_delay_ms})
        else:
            await fake.configure(invalid_bots=[bad.bot_id])
        try:
            phases.append(await gen.run_phase("bad_bot", rate, duration, weights=share_weights(gen.tenants, bad.master_id, bad_share), special_master=bad.master_id))
        finally:
            await fake.configure(slow_bots={}, invalid_bots=[])
    else:
        raise ValueError(f"неизвестный профиль: {profile}")
    return phases


# ---- сценарий планировщика ----

MAKE_DUE_SQL = """
WITH candidates AS (
    SELECT n.id, row_number() OVER (PARTITION BY n.master_id ORDER BY random()) AS rn
    FROM notifications n
    JOIN bookings b ON b.id = n.booking_id AND b.master_id = n.master_id
    JOIN masters m ON m.id = n.master_id
    WHERE m.owner_telegram_id >= $1 AND n.status = 'pending' AND n.scheduled_at > now()
      AND b.status = 'confirmed' AND b.start_datetime > now() + interval '3 hours'
), picked AS (
    SELECT id FROM candidates WHERE rn <= $2 ORDER BY random() LIMIT $3
)
UPDATE notifications SET scheduled_at = now() - interval '1 minute' WHERE id IN (SELECT id FROM picked) RETURNING id
"""


async def run_scheduler_scenario(dsn: str, fake: FakeClient, *, count: int, drain_timeout: float, masters: int, poll_interval: float = 0.5) -> dict:
    """Делает «созревшими» ~`count` уведомлений у разных мастеров и ждёт, пока планировщик приложения опустошит очередь.

    Берутся только ещё не отправленные уведомления тестовых мастеров по подтверждённым будущим записям (напоминание за 2 часа
    отправляется, лишь пока запись не началась, поэтому берутся записи не ближе 3 часов). Возвращает числа для отчёта; дубли считает
    поддельный Telegram (повторная ДОСТАВКА одного текста в один чат одним ботом)."""
    await fake.reset()
    conn = await asyncpg.connect(dsn)
    try:
        quota = math.ceil(count / max(masters, 1)) + 2
        window_start = await conn.fetchval("SELECT now()")  # время БД: «sent за время прогона» считается от него
        due_ids = [row["id"] for row in await conn.fetch(MAKE_DUE_SQL, TEST_OWNER_MIN, quota, count)]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        timeline: list[tuple[float, int, int]] = []
        counts: dict[str, int] = {}
        drained = False
        while True:
            counts = {row["status"]: row["n"] for row in await conn.fetch("SELECT status, count(*) AS n FROM notifications WHERE id = ANY($1) GROUP BY status", due_ids)}
            remaining = counts.get("pending", 0) + counts.get("sending", 0)
            elapsed = loop.time() - t0
            timeline.append((round(elapsed, 2), remaining, counts.get("sent", 0)))
            if remaining == 0:
                drained = True
                break
            if elapsed > drain_timeout:
                break
            await asyncio.sleep(poll_interval)
        sent_in_window = await conn.fetchval(
            """SELECT count(*) FROM notifications n JOIN masters m ON m.id = n.master_id
               WHERE m.owner_telegram_id >= $1 AND n.status = 'sent' AND n.sent_at >= $2""", TEST_OWNER_MIN, window_start
        )
        deactivated = await conn.fetchval(
            "SELECT count(*) FROM master_bots WHERE NOT is_active AND master_id IN (SELECT id FROM masters WHERE owner_telegram_id >= $1)", TEST_OWNER_MIN
        )
    finally:
        await conn.close()
    stats = await fake.stats()
    first_progress = next((t for t, remaining, _ in timeline if remaining < len(due_ids)), None)
    return {
        "made_due": len(due_ids), "drained": drained, "drain_s": timeline[-1][0] if drained else None, "waited_s": timeline[-1][0],
        "first_progress_s": first_progress, "status_counts": counts, "sent_in_window": sent_in_window, "timeline": timeline, "bots_deactivated": deactivated, "fake": stats,
    }
