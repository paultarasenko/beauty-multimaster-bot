"""Запуск прогона и отчёт.

    python -m tools.loadtest.run --profile normal --duration 60

Профили: normal (20 апдейтов/с), peak (100/с), storm (один мастер — половина трафика), bad_bot (у одного мастера «медленный» или
«недействительный» бот), scheduler (≈3000 «созревших» уведомлений у разных мастеров; сколько времени занимает опустошение очереди).
Отчёт (markdown + CSV) сохраняется в tools/loadtest/results/<дата>/. Код выхода 1, если какой-либо критерий не пройден.

Нужны переменные окружения (docker-compose.loadtest.yml задаёт их для контейнера runner): LOADTEST_DATABASE_URL (имя базы —
«loadtest…»), LOADTEST_REDIS_URL, LOADTEST_FAKE_URL, LOADTEST_APP_URL, BOT_TOKEN_ENCRYPTION_KEY.
"""

import argparse
import asyncio
import datetime as dt
import os
import random
import sys
from pathlib import Path

import aiohttp

from tools.loadtest import collectors, report
from tools.loadtest.common import asyncpg_dsn, require_loadtest_db_url
from tools.loadtest.load import (
    FakeClient,
    Generator,
    ensure_bots_active,
    load_tenants,
    run_load_profile,
    run_scheduler_scenario,
)

DEFAULT_RATES = {"normal": 20.0, "peak": 100.0, "storm": 100.0, "bad_bot": 20.0}
RESULTS_DIR = Path(__file__).resolve().parent / "results"
REACTIVATION_WAIT_S = 35  # кэш реестра ботов живёт 30 с: после включения ботов ждём, иначе приложение «молча» пропустит апдейты


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", required=True, choices=["normal", "peak", "storm", "bad_bot", "scheduler"])
    p.add_argument("--duration", type=float, default=60.0, help="длительность основной фазы, с (по умолчанию 60)")
    p.add_argument("--rate", type=float, help="апдейтов в секунду (по умолчанию: normal 20, peak 100, storm 100, bad_bot 20)")
    p.add_argument("--warmup", type=float, default=5.0, help="прогрев перед замером, с; в статистику не входит")
    p.add_argument("--baseline-duration", type=float, default=20.0, help="storm / bad_bot: длительность базовой фазы без помех, с")
    p.add_argument("--hot-share", type=float, default=0.5, help="storm: доля трафика одного мастера (по умолчанию 0.5)")
    p.add_argument("--bad-share", type=float, default=0.1, help="bad_bot: доля трафика мастера с плохим ботом (по умолчанию 0.1)")
    p.add_argument("--bad-mode", choices=["slow", "invalid"], default="slow", help="bad_bot: медленный бот (slow) или недействительный токен (invalid, ответ 401)")
    p.add_argument("--bad-delay-ms", type=float, default=8000.0, help="bad_bot slow: задержка ответа Telegram для этого бота")
    p.add_argument("--notifications", type=int, default=3000, help="scheduler: сколько уведомлений сделать «созревшими»")
    p.add_argument("--drain-timeout", type=float, default=300.0, help="scheduler: сколько ждать опустошения очереди, с")
    p.add_argument("--fake-delay-ms", type=float, default=30.0, help="задержка ответа поддельного Telegram, мс")
    p.add_argument("--fake-jitter-ms", type=float, default=20.0)
    p.add_argument("--fake-429-share", type=float, default=0.0, help="доля ответов 429 на отправку сообщений (нужна в основном для scheduler)")
    p.add_argument("--fake-403-share", type=float, default=0.0, help="доля ответов 403 «bot was blocked by the user»")
    p.add_argument("--retry-after", type=int, default=1, help="retry_after в ответах 429, с")
    p.add_argument("--client-timeout", type=float, default=30.0, help="таймаут запроса генератора к webhook, с (таймаут = ошибка)")
    p.add_argument("--random-seed", type=int, default=1)
    p.add_argument("--out-prefix", help="префикс файлов отчёта (по умолчанию results/<дата>/<ЧЧММСС>_<профиль>)")
    p.add_argument("--docker-stats-file", help="файл с замерами docker stats (его пишет tools/loadtest/run.sh)")
    args = p.parse_args(argv)
    if args.duration <= 0 or args.warmup < 0 or not 0 < args.hot_share < 1 or not 0 < args.bad_share < 1:
        p.error("--duration > 0, --warmup ≥ 0, доли — от 0 до 1 (не включая)")
    return args


async def http_status(http: aiohttp.ClientSession, url: str) -> int | None:
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            return response.status
    except Exception:
        return None


def out_prefix(args: argparse.Namespace) -> Path:
    if args.out_prefix:
        prefix = Path(args.out_prefix)
    else:
        now = dt.datetime.now()
        prefix = RESULTS_DIR / now.strftime("%Y-%m-%d") / f"{now.strftime('%H%M%S')}_{args.profile}"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    return prefix


def infra_summary(pg: collectors.PgSampler, rd: collectors.RedisSampler, docker_text: str | None, since: float, until: float) -> dict:
    def peak(rows, name):
        values = collectors.series(rows, name)
        return max(values) if values else None

    info: dict = {}
    total, active, idle_tx = peak(pg.rows, "pg_total"), peak(pg.rows, "pg_active"), peak(pg.rows, "pg_idle_in_tx")
    info["PostgreSQL: соединений к базе (макс.)"] = f"{total:.0f} из {pg.max_connections} (работают одновременно: {active:.0f}; «idle in transaction»: {idle_tx:.0f}, самая долгая — {peak(pg.rows, 'pg_max_idle_tx_s'):.1f} с)" if total is not None else "нет замеров"
    used, keys = peak(rd.rows, "redis_used_mb"), collectors.series(rd.rows, "redis_keys")
    info["Redis: память (макс.), ключей в конце"] = f"{used:.1f} МБ, {keys[-1]:.0f} ключей" if used is not None and keys else "нет замеров"
    if docker_text:
        docker = collectors.parse_docker_stats(docker_text, since, until)
        for name, d in sorted(docker.items()):
            info[f"Контейнер {name}"] = f"CPU средний {d['cpu_avg']:.0f}% / макс {d['cpu_max']:.0f}% (100% = одно ядро), память макс {d['mem_max_mib']:.0f} МиБ"
        if not docker:
            info["CPU/память контейнеров"] = "в файле нет замеров за время прогона"
    else:
        info["CPU/память контейнеров"] = "нет данных (запускайте через tools/loadtest/run.sh — он пишет docker stats)"
    return info


async def amain(args: argparse.Namespace) -> int:
    dsn_url = require_loadtest_db_url()  # защита: проверка имени базы до любых действий
    dsn = asyncpg_dsn(dsn_url)
    app_url = os.environ.get("LOADTEST_APP_URL", "http://app:8080").rstrip("/")
    fake_url = os.environ.get("LOADTEST_FAKE_URL", "http://fake-telegram:8081").rstrip("/")
    redis_url = os.environ.get("LOADTEST_REDIS_URL", "redis://redis-loadtest:6379/0")
    poll_seconds = float(os.environ.get("LOADTEST_POLL_SECONDS", "60"))
    rate = args.rate or DEFAULT_RATES.get(args.profile, 0.0)
    prefix = out_prefix(args)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as http:
        fake = FakeClient(http, fake_url)
        ready_before = await http_status(http, f"{app_url}/ready")
        if ready_before != 200:
            raise SystemExit(f"Приложение не готово (GET {app_url}/ready → {ready_before}). Поднимите стенд: см. tools/loadtest/README.md")
        try:
            await fake.reset(config=True)
        except Exception:
            raise SystemExit(f"Поддельный Telegram недоступен на {fake_url}") from None
        if await ensure_bots_active(dsn):
            print(f"Часть ботов была отключена прошлым прогоном — включены; ждём {REACTIVATION_WAIT_S} с (кэш реестра)…", flush=True)
            await asyncio.sleep(REACTIVATION_WAIT_S)
        tenants = await load_tenants(dsn, os.environ.get("BOT_TOKEN_ENCRYPTION_KEY", ""))
        if not tenants:
            raise SystemExit("Тестовых мастеров нет: сначала python -m tools.loadtest.seed --masters 100")
        await fake.configure(
            delay_ms=args.fake_delay_ms, jitter_ms=args.fake_jitter_ms, rate_429=args.fake_429_share, rate_403=args.fake_403_share, retry_after=args.retry_after
        )
        await fake.reset()

        loop = asyncio.get_running_loop()
        started_wall = dt.datetime.now().timestamp()
        origin = loop.time()
        pg, rd = collectors.PgSampler(dsn, origin), collectors.RedisSampler(redis_url, origin)
        await pg.start()
        await rd.start()
        print(f"Профиль {args.profile}: {len(tenants)} мастеров" + (f", {rate:g} апдейтов/с, {args.duration:g} с" if args.profile != "scheduler" else f", {args.notifications} уведомлений"), flush=True)

        data: dict = {"poll_seconds": poll_seconds, "summaries": {}, "phases": []}
        gen = None
        scheduler = None
        try:
            if args.profile == "scheduler":
                scheduler = await run_scheduler_scenario(dsn, fake, count=args.notifications, drain_timeout=args.drain_timeout, masters=len(tenants))
                data["scheduler"], data["fake"] = scheduler, scheduler["fake"]
            else:
                gen = Generator(tenants, http, fake, app_url, rng=random.Random(args.random_seed), timeout=args.client_timeout)
                phases = await run_load_profile(
                    gen, fake, args.profile, rate=rate, duration=args.duration, warmup=args.warmup, baseline_duration=args.baseline_duration,
                    hot_share=args.hot_share, bad_share=args.bad_share, bad_mode=args.bad_mode, bad_delay_ms=args.bad_delay_ms,
                )
                data["fake"] = await fake.stats()
                data["fake_total_calls"] = data["fake"]["total_calls"]
                data["notes"], data["internal_errors"] = dict(gen.notes), gen.internal_errors
                for phase in phases:
                    own = [s for s in gen.samples if s.phase == phase.name]
                    summary = report.summarize(own)
                    data["summaries"][phase.name] = summary
                    if phase.name in ("storm", "bad_bot"):
                        for group in ("others", "special"):
                            data["summaries"][f"{phase.name}:{group}"] = report.summarize(s for s in own if s.group == group)
                    data["phases"].append(
                        {"name": phase.name, "target_rate": phase.target_rate, "sessions_skipped": phase.sessions_skipped,
                         "achieved_rate": summary["count"] / phase.duration if phase.duration else None}
                    )
        finally:
            await pg.stop()
            await rd.stop()
        until_wall = dt.datetime.now().timestamp()
        data["ready_after"] = await http_status(http, f"{app_url}/ready")

    docker_text = None
    if args.docker_stats_file and Path(args.docker_stats_file).exists():
        docker_text = Path(args.docker_stats_file).read_text(encoding="utf-8", errors="replace")
    criteria = report.evaluate(args.profile, data)
    warns = report.warnings(args.profile, data)
    infra = infra_summary(pg, rd, docker_text, started_wall, until_wall)
    header = {
        "Профиль": args.profile,
        "Дата": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Мастеров с ботами": len(tenants),
        "Параметры": (f"{rate:g} апдейтов/с, основная фаза {args.duration:g} с, прогрев {args.warmup:g} с" if args.profile != "scheduler" else f"{args.notifications} уведомлений, лимит ожидания {args.drain_timeout:g} с, интервал планировщика {poll_seconds:g} с")
        + f"; поддельный Telegram: задержка {args.fake_delay_ms:g}±{args.fake_jitter_ms:g} мс, 429: {args.fake_429_share:g}, 403: {args.fake_403_share:g}"
        + (f"; bad_bot: {args.bad_mode}, доля {args.bad_share:g}" if args.profile == "bad_bot" else "")
        + (f"; storm: доля горячего мастера {args.hot_share:g}" if args.profile == "storm" else ""),
        "Вызовов Bot API (поддельный Telegram)": data["fake"]["total_calls"],
    }
    markdown = report.render_markdown(f"Нагрузочный прогон: {args.profile}", header, criteria, data["summaries"], infra, warns, scheduler)

    prefix.with_name(prefix.name + "_report.md").write_text(markdown, encoding="utf-8")
    summary_rows = [(group, k, "" if v is None else (f"{v:.1f}" if isinstance(v, float) else v)) for group, s in data["summaries"].items() for k, v in s.items() if k != "statuses"]
    summary_rows += [("criteria", c.name, c.status) for c in criteria]
    report.write_csv(prefix.with_name(prefix.name + "_summary.csv"), ["group", "metric", "value"], summary_rows)
    if gen is not None:
        report.write_csv(
            prefix.with_name(prefix.name + "_requests.csv"), ["t_s", "phase", "group", "master_id", "step", "status", "latency_ms", "lag_ms", "error"],
            ((f"{s.t:.3f}", s.phase, s.group, s.master_id, s.step, s.status, f"{s.latency_ms:.1f}", f"{s.lag_ms:.1f}", s.error) for s in gen.samples),
        )
    if scheduler is not None:
        report.write_csv(prefix.with_name(prefix.name + "_queue.csv"), ["t_s", "remaining", "sent"], scheduler["timeline"])
    report.write_csv(prefix.with_name(prefix.name + "_infra.csv"), ["t_s", "metric", "value"], sorted(pg.rows + rd.rows))
    print(markdown)
    print(f"Файлы отчёта: {prefix.parent}/{prefix.name}_*", flush=True)
    return 1 if any(c.status == "FAIL" for c in criteria) else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
