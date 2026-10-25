"""Сводка прогона: перцентили, критерии «прошёл / не прошёл», markdown и CSV. Чистые функции без сети и базы."""

import csv
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tools.loadtest.common import percentile

# ---- пороги критериев (обоснование — в README и в «why» каждого критерия) ----
P95_NORMAL_MS = 500.0  # профиль normal: 95 % апдейтов обрабатываются быстрее полсекунды (пользователь ждёт ответа в чате)
DEGRADATION_FACTOR = 2.0  # storm / bad_bot: p95 остальных мастеров не больше чем в 2 раза хуже базового
NOISE_FLOOR_MS = 25.0  # разница меньше этого — шум измерения: при p95 в 10 мс «в 2 раза» значит всего 10 мс
DRAIN_TICKS_ALLOWED = 2  # очередь должна опустеть не позже, чем через столько интервалов планировщика + запас
DRAIN_MARGIN_S = 60.0
LAG_WARN_MS = 50.0


@dataclass
class Criterion:
    name: str
    status: str  # PASS | FAIL | INFO
    value: str
    threshold: str
    why: str


def summarize(samples: Iterable) -> dict:
    """Числа по набору замеров (Sample): число, ошибки, перцентили времени ответа и отставание генератора."""
    items = list(samples)
    latencies = [s.latency_ms for s in items]
    statuses = Counter(s.status for s in items)
    ok = statuses.get(200, 0)
    span = (max(s.t + s.latency_ms / 1000 for s in items) - min(s.t for s in items)) if items else 0.0
    return {
        "count": len(items),
        "errors": len(items) - ok,
        "transport_errors": statuses.get(0, 0),
        "statuses": dict(sorted(statuses.items())),
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "max": max(latencies) if latencies else None,
        "lag_p95": percentile([s.lag_ms for s in items], 95),
        "lag_max": max((s.lag_ms for s in items), default=None),
        "ok_per_s": ok / span if span > 0 else None,
    }


def degradation_ok(baseline_p95: float | None, p95: float | None) -> bool:
    """Не хуже чем в 2 раза (или хуже менее чем на 25 мс — шум измерения)."""
    if baseline_p95 is None or p95 is None:
        return False
    return p95 <= DEGRADATION_FACTOR * baseline_p95 or p95 - baseline_p95 < NOISE_FLOOR_MS


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} мс"


def evaluate(profile: str, data: dict) -> list[Criterion]:
    """`data`: summaries (имя группы -> summarize), scheduler (результат сценария), ready_after (HTTP-код /ready), poll_seconds."""
    result: list[Criterion] = []
    summaries = data.get("summaries", {})

    def errors(name: str, group: str, why: str) -> None:
        s = summaries.get(group)
        if s is None:
            return
        result.append(Criterion(name, "PASS" if s["errors"] == 0 else "FAIL", f"{s['errors']} из {s['count']} (не-200 или обрыв/таймаут)", "0", why))

    if profile == "normal":
        errors("Ошибок нет", "main", "Любой ответ не 200 или обрыв/таймаут — потерянный или повторно доставляемый Telegram'ом апдейт.")
        p95 = summaries["main"]["p95"]
        result.append(Criterion("p95 ответа webhook", "PASS" if p95 is not None and p95 < P95_NORMAL_MS else "FAIL", _ms(p95), f"< {P95_NORMAL_MS:.0f} мс",
                                "Обычная нагрузка (20 апдейтов/с): клиент не должен замечать задержку; 500 мс — порог, после которого чат «подвисает»."))
    elif profile == "peak":
        errors("Ошибок нет", "main", "На пике ошибок и обрывов быть не должно.")
        result.append(Criterion("p95 ответа webhook", "INFO", _ms(summaries["main"]["p95"]), "без порога", "Порога на пике нет: цифры сравнивают с normal и используют в этапе 12C."))
    elif profile in ("storm", "bad_bot"):
        phase, special = ("storm", "hot") if profile == "storm" else ("bad_bot", "bad")
        errors("Ошибок нет (базовая фаза)", "baseline", "Без помех ошибок быть не должно.")
        if profile == "storm":
            errors("Ошибок нет (весь трафик шторма)", phase, "Ответ 200 обязателен и для горячего мастера, и для остальных.")
        else:
            errors("Ошибок нет (остальные мастера)", f"{phase}:others", "Плохой бот одного мастера не должен давать ошибки у остальных.")
        base, others = summaries["baseline"]["p95"], summaries[f"{phase}:others"]["p95"]
        result.append(Criterion("p95 остальных мастеров не хуже чем в 2 раза", "PASS" if degradation_ok(base, others) else "FAIL",
                                f"{_ms(others)} против базовых {_ms(base)}", f"≤ {DEGRADATION_FACTOR:.0f}× базового (или разница < {NOISE_FLOOR_MS:.0f} мс)",
                                "Изоляция мастеров: чужая беда не должна замедлять остальных. Разница меньше 25 мс — шум измерения."))
        special_summary = summaries.get(f"{phase}:{'special'}")
        if special_summary:
            result.append(Criterion(f"Для справки: {'горячий' if profile == 'storm' else 'плохой'} мастер", "INFO",
                                    f"p95 {_ms(special_summary['p95'])}, ошибок {special_summary['errors']} из {special_summary['count']}", "—", "Не критерий: показывает, как приложение ведёт себя с самим «проблемным» мастером."))
    elif profile == "scheduler":
        sch = data["scheduler"]
        limit = DRAIN_TICKS_ALLOWED * data.get("poll_seconds", 60) + DRAIN_MARGIN_S
        drained = sch["drained"]
        result.append(Criterion("Очередь опустела", "PASS" if drained else "FAIL", f"за {sch['drain_s']:.0f} с" if drained else f"нет, за {sch['waited_s']:.0f} с осталось {sch['timeline'][-1][1]}",
                                f"≤ {limit:.0f} с", f"Планировщик тикает раз в {data.get('poll_seconds', 60)} с; допускаем {DRAIN_TICKS_ALLOWED} тика + {DRAIN_MARGIN_S:.0f} с на отправку при лимите 25 сообщений/с на бота."))
        result.append(Criterion("Время опустошения в пределах нормы", "PASS" if drained and sch["drain_s"] <= limit else "FAIL", f"{sch['drain_s']:.0f} с" if drained else "—", f"≤ {limit:.0f} с",
                                "Напоминание, пришедшее на минуты позже, теряет смысл (особенно «за 2 часа»)."))
        fake, counts = sch["fake"], sch["status_counts"]
        result.append(Criterion("Дублей отправки нет", "PASS" if fake["duplicate_deliveries"] == 0 else "FAIL", f"{fake['duplicate_deliveries']} повторных доставок", "0",
                                "Один и тот же текст не должен дважды дойти до одного чата одним ботом (по данным поддельного Telegram)."))
        accounted = counts.get("sent", 0) + counts.get("failed", 0) + counts.get("cancelled", 0)
        result.append(Criterion("Всё обработано", "PASS" if accounted == sch["made_due"] else "FAIL", f"{accounted} из {sch['made_due']} (sent {counts.get('sent', 0)}, failed {counts.get('failed', 0)}, cancelled {counts.get('cancelled', 0)})",
                                "= созревшим", "Ни одно уведомление не потеряно и не застряло."))
        sent_window = sch["sent_in_window"]
        result.append(Criterion("Доставок столько, сколько отметок «sent»", "PASS" if fake["delivered_messages"] == sent_window else "FAIL",
                                f"доставлено {fake['delivered_messages']}, sent в БД за время прогона {sent_window}", "равно",
                                "Считаются все напоминания, ушедшие за время прогона (в том числе наступившие сами). Расхождение значит дубль или отметку «sent» без отправки."))
        result.append(Criterion("Боты не отключены", "PASS" if sch["bots_deactivated"] == 0 else "FAIL", f"{sch['bots_deactivated']}", "0", "Без 401 планировщик не должен отключать ботов."))
    if profile != "scheduler" or data.get("ready_after") is not None:
        ready = data.get("ready_after")
        result.append(Criterion("Приложение живо после прогона (/ready)", "PASS" if ready == 200 else "FAIL", str(ready), "200", "База и Redis отвечают, процесс не завис."))
    return result


def warnings(profile: str, data: dict) -> list[str]:
    out: list[str] = []
    for name, s in data.get("summaries", {}).items():
        if s["lag_p95"] is not None and s["lag_p95"] > LAG_WARN_MS:
            out.append(f"Генератор отставал от расписания (p95 {s['lag_p95']:.0f} мс в группе «{name}»): цифры занижены, запустите на менее загруженной машине.")
    for phase in data.get("phases", []):
        if phase["sessions_skipped"]:
            out.append(f"Фаза «{phase['name']}»: {phase['sessions_skipped']} сессий пропущено — у мастера не нашлось свободного клиента.")
        if phase["target_rate"] and phase.get("achieved_rate") and phase["achieved_rate"] < 0.9 * phase["target_rate"]:
            out.append(f"Фаза «{phase['name']}»: фактически {phase['achieved_rate']:.1f} апдейтов/с при заданных {phase['target_rate']:.0f}.")
    if profile != "scheduler" and data.get("fake_total_calls") == 0:
        out.append("Поддельный Telegram не получил ни одного вызова: боты, вероятно, неактивны или приложение не обрабатывает апдейты.")
    for note, count in data.get("notes", {}).items():
        out.append(f"Сессий оборвано: {count} — {note}.")
    if data.get("internal_errors"):
        out.append(f"Внутренних ошибок генератора: {data['internal_errors']} (см. вывод).")
    if data.get("fake", {}).get("unknown_methods"):
        out.append(f"Приложение вызвало методы, которых нет в списке поддельного Telegram: {data['fake']['unknown_methods']}")
    return out


def render_markdown(title: str, header: dict, criteria: list[Criterion], summaries: dict, infra: dict, warns: list[str], scheduler: dict | None) -> str:
    lines = [f"# {title}", ""]
    lines += [f"- {k}: {v}" for k, v in header.items()]
    lines += ["", "## Критерии", "", "| Критерий | Итог | Факт | Порог |", "|---|---|---|---|"]
    lines += [f"| {c.name} | **{c.status}** | {c.value} | {c.threshold} |" for c in criteria]
    lines += ["", "Обоснование порогов:"] + [f"- {c.name}: {c.why}" for c in criteria if c.status != "INFO"]
    if summaries:
        lines += ["", "## Время ответа webhook", "", "| Группа | Апдейтов | Не-200 | p50 | p95 | p99 | max | ответов 200/с |", "|---|---|---|---|---|---|---|---|"]
        for name, s in summaries.items():
            rate = "—" if s["ok_per_s"] is None else f"{s['ok_per_s']:.1f}"
            lines.append(f"| {name} | {s['count']} | {s['errors']} | {_ms(s['p50'])} | {_ms(s['p95'])} | {_ms(s['p99'])} | {_ms(s['max'])} | {rate} |")
    if scheduler:
        counts = scheduler["status_counts"]
        lines += ["", "## Планировщик", "",
                  f"- созрело уведомлений: {scheduler['made_due']}; sent {counts.get('sent', 0)}, failed {counts.get('failed', 0)}, cancelled {counts.get('cancelled', 0)}, осталось {scheduler['timeline'][-1][1]}",
                  f"- до первого прогресса (ожидание тика): {scheduler['first_progress_s'] if scheduler['first_progress_s'] is not None else '—'} с; полное опустошение: {scheduler['drain_s'] if scheduler['drain_s'] is not None else 'не опустела'} с",
                  f"- вызовов sendMessage в поддельный Telegram: {scheduler['fake']['methods'].get('sendMessage', {}).get('calls', 0)}, доставлено {scheduler['fake']['delivered_messages']}, повторных доставок {scheduler['fake']['duplicate_deliveries']}"]
    lines += ["", "## Инфраструктура", ""] + [f"- {k}: {v}" for k, v in infra.items()]
    if warns:
        lines += ["", "## Предупреждения", ""] + [f"- {w}" for w in warns]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, header: list[str], rows: Iterable[Iterable]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
