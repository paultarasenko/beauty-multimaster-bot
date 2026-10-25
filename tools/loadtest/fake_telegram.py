"""Поддельный Telegram Bot API для нагрузочного стенда (aiohttp).

    python -m tools.loadtest.fake_telegram --port 8081

Приложение направляют сюда через TELEGRAM_API_BASE=http://fake-telegram:8081: запросы `POST /bot<токен>/<метод>`.
Токены в статистику и лог НЕ попадают: всё считается по id бота (число до двоеточия).

Служебные адреса:
  GET  /__stats            счётчики вызовов по методам и ботам, доставленные сообщения и повторы (дубли)
  POST /__reset            сброс счётчиков (?config=1 — ещё и настроек)
  GET/POST /__config       настройки на лету (JSON, частичное обновление):
        delay_ms, jitter_ms        задержка каждого ответа
        rate_429, retry_after      доля ответов 429 «Too Many Requests» на отправку сообщений и значение retry_after (с)
        rate_403                   доля ответов 403 «Forbidden: bot was blocked by the user» на отправку сообщений
        slow_bots {"<bot_id>": ms} «медленные» боты: эта задержка вместо delay_ms
        invalid_bots [bot_id, ...] «недействительные» боты: на любой метод 401 Unauthorized
  GET  /__last?bot_id=&chat_id=   последнее сообщение бота в чате с inline-клавиатурой (по ней нагрузка берёт настоящие callback_data)

Методы, которые использует приложение (найдены по коду): sendMessage, editMessageText, editMessageReplyMarkup,
editMessageCaption, editMessageMedia, sendPhoto, sendDocument, deleteMessage, answerCallbackQuery, getMe, getChat,
getWebhookInfo, setWebhook, deleteWebhook, getUpdates. Неизвестный метод получает {"ok": true, "result": true} и попадает в лог и
в «unknown_methods» статистики.
"""

import argparse
import asyncio
import hashlib
import itertools
import json
import logging
import random
import time
from collections import Counter

from aiohttp import web

logger = logging.getLogger("fake_telegram")

SEND_METHODS = {"sendMessage", "sendPhoto", "sendDocument", "sendMediaGroup", "sendVideo", "sendAnimation", "sendLocation", "sendContact"}
EDIT_METHODS = {"editMessageText", "editMessageReplyMarkup", "editMessageCaption", "editMessageMedia"}
TRUE_METHODS = {"answerCallbackQuery", "deleteMessage", "setWebhook", "deleteWebhook", "setMyCommands", "sendChatAction", "pinChatMessage", "unpinChatMessage"}
KNOWN = SEND_METHODS | EDIT_METHODS | TRUE_METHODS | {"getMe", "getChat", "getWebhookInfo", "getUpdates"}
MAX_TRACKED_DELIVERIES = 2_000_000  # предохранитель по памяти для учёта повторов


def default_config(delay_ms: float = 30.0, jitter_ms: float = 20.0) -> dict:
    return {"delay_ms": delay_ms, "jitter_ms": jitter_ms, "rate_429": 0.0, "retry_after": 1, "rate_403": 0.0, "slow_bots": {}, "invalid_bots": []}


class FakeState:
    """Всё изменяемое состояние сервера (в объекте, а не в модуле: два экземпляра в тестах не мешают друг другу)."""

    def __init__(self, delay_ms: float = 30.0, jitter_ms: float = 20.0, seed: int = 1):
        self._defaults = (delay_ms, jitter_ms)
        self.rng = random.Random(seed)
        self.config = default_config(delay_ms, jitter_ms)
        self.message_ids = itertools.count(1000)
        self.markup_seq = itertools.count(1)
        self.reset()

    def reset(self, *, config: bool = False) -> None:
        if config:
            self.config = default_config(*self._defaults)
        self.since = time.time()
        self.calls: Counter = Counter()  # (метод, исход) -> число
        self.by_bot: Counter = Counter()  # (bot_id, метод) -> число
        self.unknown: Counter = Counter()
        self.deliveries: Counter = Counter()  # (bot_id, chat_id, хэш текста) -> сколько раз ДОСТАВЛЕНО
        self.delivered_total = 0
        self.tracking_overflow = False
        self.last_markup: dict[tuple[int, int], dict] = {}
        self.inflight = 0
        self.inflight_peak = 0

    def apply_config(self, patch: dict) -> None:
        known = {"delay_ms", "jitter_ms", "rate_429", "retry_after", "rate_403", "slow_bots", "invalid_bots"}
        unknown = set(patch) - known
        if unknown:
            raise ValueError(f"неизвестные настройки: {sorted(unknown)}")
        merged = dict(self.config)
        for key, value in patch.items():
            if key in ("delay_ms", "jitter_ms", "rate_429", "rate_403"):
                value = float(value)
                if value < 0 or (key.startswith("rate_") and value > 1):
                    raise ValueError(f"{key}: недопустимое значение")
            elif key == "retry_after":
                value = int(value)
            elif key == "slow_bots":
                value = {str(k): float(v) for k, v in dict(value).items()}
            elif key == "invalid_bots":
                value = [int(v) for v in value]
            merged[key] = value
        if merged["rate_429"] + merged["rate_403"] > 1:
            raise ValueError("rate_429 + rate_403 не может быть больше 1")
        self.config = merged

    def stats(self) -> dict:
        methods: dict[str, dict] = {}
        for (method, outcome), count in sorted(self.calls.items()):
            methods.setdefault(method, {"calls": 0})
            methods[method][outcome] = count
            methods[method]["calls"] += count
        bots: dict[str, dict] = {}
        for (bot_id, method), count in self.by_bot.items():
            bots.setdefault(str(bot_id), {})[method] = count
        duplicates = sum(count - 1 for count in self.deliveries.values() if count > 1)
        return {
            "since_unix": self.since,
            "uptime_s": round(time.time() - self.since, 1),
            "total_calls": sum(self.calls.values()),
            "methods": methods,
            "bots": bots,
            "delivered_messages": self.delivered_total,
            "unique_deliveries": len(self.deliveries),
            "duplicate_deliveries": duplicates,
            "tracking_overflow": self.tracking_overflow,
            "unknown_methods": dict(self.unknown),
            "inflight_peak": self.inflight_peak,
            "config": self.config,
        }


def _error(code: int, description: str, **parameters) -> web.Response:
    body: dict = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return web.json_response(body, status=code)


def _chat_id(params) -> int:
    try:
        return int(params.get("chat_id", 0))
    except (TypeError, ValueError):
        return 0


def _message(bot_id: int, chat_id: int, params, message_id: int | None = None) -> dict:
    return {
        "message_id": message_id if message_id is not None else 0,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": bot_id, "is_bot": True, "first_name": f"LoadBot {bot_id}", "username": f"loadbot{bot_id}_bot"},
        "text": params.get("text") or params.get("caption") or "",
    }


async def _read_params(request: web.Request) -> dict:
    """Параметры метода: aiogram шлёт multipart/form-data, но JSON и query тоже принимаем. Файлы не читаем."""
    params: dict = dict(request.query)
    ctype = request.content_type or ""
    try:
        if ctype == "application/json":
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        elif request.can_read_body:
            for key, value in (await request.post()).items():
                params[key] = value if isinstance(value, str) else "<file>"
    except Exception:
        logger.debug("не удалось разобрать тело запроса", exc_info=True)
    return params


def _markup(params: dict) -> dict | None:
    raw = params.get("reply_markup")
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) and "inline_keyboard" in value else None


async def handle_bot_method(request: web.Request) -> web.Response:
    state: FakeState = request.app["state"]
    token, method = request.match_info["token"], request.match_info["method"]
    head = token.split(":", 1)[0]
    if not head.isdigit():
        return _error(401, "Unauthorized")
    bot_id = int(head)
    state.inflight += 1
    state.inflight_peak = max(state.inflight_peak, state.inflight)
    try:
        return await _handle(state, request, bot_id, method)
    finally:
        state.inflight -= 1


async def _handle(state: FakeState, request: web.Request, bot_id: int, method: str) -> web.Response:
    cfg = state.config
    params = await _read_params(request)
    state.by_bot[(bot_id, method)] += 1

    delay_ms = cfg["slow_bots"].get(str(bot_id), cfg["delay_ms"])
    if cfg["jitter_ms"]:
        delay_ms += state.rng.uniform(-cfg["jitter_ms"], cfg["jitter_ms"])
    if method == "getUpdates":
        delay_ms = min(float(params.get("timeout") or 0), 2.0) * 1000  # длинный опрос: ждём недолго и отвечаем пустым списком
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)

    if bot_id in cfg["invalid_bots"]:
        state.calls[(method, "401")] += 1
        return _error(401, "Unauthorized")

    if method in SEND_METHODS:
        roll = state.rng.random()
        if roll < cfg["rate_429"]:
            state.calls[(method, "429")] += 1
            return _error(429, f"Too Many Requests: retry after {cfg['retry_after']}", retry_after=cfg["retry_after"])
        if roll < cfg["rate_429"] + cfg["rate_403"]:
            state.calls[(method, "403")] += 1
            return _error(403, "Forbidden: bot was blocked by the user")

    state.calls[(method, "ok")] += 1
    if method not in KNOWN:
        if state.unknown[method] == 0:
            logger.warning("неизвестный метод Bot API %s (ответ ok:true, result:true)", method)
        state.unknown[method] += 1

    if method == "getMe":
        result = {
            "id": bot_id, "is_bot": True, "first_name": f"LoadBot {bot_id}", "username": f"loadbot{bot_id}_bot",
            "can_join_groups": True, "can_read_all_group_messages": False, "supports_inline_queries": False,
        }
    elif method == "getChat":
        chat_id = _chat_id(params)
        result = {"id": chat_id, "type": "private", "first_name": "Owner", "username": f"owner_{chat_id}", "accent_color_id": 0, "max_reaction_count": 11}
    elif method == "getWebhookInfo":
        result = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
    elif method == "getUpdates":
        result = []
    elif method in SEND_METHODS:
        chat_id = _chat_id(params)
        result = _message(bot_id, chat_id, params, next(state.message_ids))
        _remember(state, bot_id, chat_id, result["message_id"], params)
        if method == "sendMessage":
            _count_delivery(state, bot_id, chat_id, params.get("text", ""))
    elif method in EDIT_METHODS:
        if "inline_message_id" in params:
            result = True
        else:
            chat_id = _chat_id(params)
            try:
                message_id = int(params.get("message_id", 0))
            except ValueError:
                message_id = 0
            result = _message(bot_id, chat_id, params, message_id)
            _remember(state, bot_id, chat_id, message_id, params)
    else:
        result = True
    return web.json_response({"ok": True, "result": result})


def _remember(state: FakeState, bot_id: int, chat_id: int, message_id: int, params: dict) -> None:
    markup = _markup(params)
    if markup is not None:
        state.last_markup[(bot_id, chat_id)] = {
            "seq": next(state.markup_seq), "message_id": message_id, "text": params.get("text", ""), "reply_markup": markup,
        }


def _count_delivery(state: FakeState, bot_id: int, chat_id: int, text: str) -> None:
    state.delivered_total += 1
    key = (bot_id, chat_id, hashlib.sha1(text.encode()).hexdigest()[:16])
    if key not in state.deliveries and len(state.deliveries) >= MAX_TRACKED_DELIVERIES:
        state.tracking_overflow = True
        return
    state.deliveries[key] += 1


async def handle_stats(request: web.Request) -> web.Response:
    return web.json_response(request.app["state"].stats())


async def handle_reset(request: web.Request) -> web.Response:
    request.app["state"].reset(config=request.query.get("config") == "1")
    return web.json_response({"ok": True})


async def handle_config(request: web.Request) -> web.Response:
    state: FakeState = request.app["state"]
    if request.method in ("POST", "PUT"):
        try:
            state.apply_config(await request.json())
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response(state.config)


async def handle_last(request: web.Request) -> web.Response:
    state: FakeState = request.app["state"]
    try:
        key = (int(request.query["bot_id"]), int(request.query["chat_id"]))
    except (KeyError, ValueError):
        return web.json_response({"ok": False, "error": "нужны bot_id и chat_id"}, status=400)
    return web.json_response(state.last_markup.get(key))


async def handle_file(request: web.Request) -> web.Response:
    return web.Response(status=404, text="no files in fake telegram")


def create_app(state: FakeState | None = None) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["state"] = state or FakeState()
    app.router.add_get("/__stats", handle_stats)
    app.router.add_post("/__reset", handle_reset)
    app.router.add_route("*", "/__config", handle_config)
    app.router.add_get("/__last", handle_last)
    app.router.add_route("GET", "/file/bot{token}/{path:.*}", handle_file)
    app.router.add_route("*", "/bot{token}/{method}", handle_bot_method)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--delay-ms", type=float, default=30.0, help="задержка ответа по умолчанию (настоящий Telegram — десятки миллисекунд)")
    parser.add_argument("--jitter-ms", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("fake Telegram: порт %s, задержка %s±%s мс", args.port, args.delay_ms, args.jitter_ms)
    web.run_app(create_app(FakeState(args.delay_ms, args.jitter_ms, args.seed)), host=args.host, port=args.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
