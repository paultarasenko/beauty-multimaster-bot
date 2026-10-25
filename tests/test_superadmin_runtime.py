"""build_runtime можно вызывать много раз в одном процессе: роутер суперадмина создаётся заново при каждой сборке."""

from unittest.mock import MagicMock

from app.config.settings import settings
from app.superadmin.handlers import build_router
from app.superadmin.runtime import build_runtime


def test_build_runtime_twice_does_not_fail(monkeypatch):
    monkeypatch.setattr(settings, "superadmin_bot_token", "123456:" + "A" * 35)
    monkeypatch.setattr(settings, "superadmin_telegram_ids", "111")
    first = build_runtime(MagicMock())
    second = build_runtime(MagicMock())  # раньше: RuntimeError: Router is already attached to <Dispatcher ...>
    assert first is not None and second is not None
    assert first.dp is not second.dp


def test_build_router_returns_new_router_each_time():
    a, b = build_router(), build_router()
    assert a is not b and a.parent_router is None and b.parent_router is None
