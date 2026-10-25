"""Автоматическая переустановка webhook в проверке ботов мастеров (SUPERADMIN_WEBHOOK_AUTO_REFRESH)."""

import pytest

from app.multibot import management
from app.superadmin import health as health_module
from app.superadmin.health import STATE_OK, STATE_PROBLEM, WebhookHealthMonitor


class FakeAlerts:
    def __init__(self):
        self.sent = []
        self.cleared = []

    async def notify(self, kind, key, text):
        self.sent.append((kind, key))
        return True

    def clear(self, kind, key):
        self.cleared.append((kind, key))


def _check(**kw):
    base = dict(master_id=1, telegram_bot_id=111, is_active=True, bot_username="b", token_valid=True)
    base.update(kw)
    return management.BotCheck(**base)


def _monitor(alerts, auto_refresh):
    return WebhookHealthMonitor(
        None, None, alerts, public_base_url="https://x.example", interval_seconds=600, auto_refresh=auto_refresh
    )


@pytest.fixture
def refresh_calls(monkeypatch):
    calls = []

    async def fake_refresh(session_factory, cipher, **kw):
        calls.append(kw)

    monkeypatch.setattr(health_module.management, "refresh_webhook", fake_refresh)
    return calls


async def test_mismatch_is_refreshed_without_alert(refresh_calls):
    alerts = FakeAlerts()
    monitor = _monitor(alerts, auto_refresh=True)
    await monitor.apply(_check(webhook_registered=True, webhook_matches=False))
    assert [c["master_id"] for c in refresh_calls] == [1]
    assert refresh_calls[0]["public_base_url"] == "https://x.example"
    assert alerts.sent == []
    assert monitor.state_of(111) == STATE_OK


async def test_missing_webhook_is_refreshed(refresh_calls):
    alerts = FakeAlerts()
    monitor = _monitor(alerts, auto_refresh=True)
    await monitor.apply(_check(webhook_registered=False))
    assert len(refresh_calls) == 1 and alerts.sent == []


async def test_disabled_by_default_alerts_only(refresh_calls):
    alerts = FakeAlerts()
    monitor = _monitor(alerts, auto_refresh=False)
    await monitor.apply(_check(webhook_registered=True, webhook_matches=False))
    assert refresh_calls == []
    assert len(alerts.sent) == 1
    assert monitor.state_of(111) == STATE_PROBLEM


async def test_failed_refresh_falls_back_to_alert(monkeypatch):
    async def failing(*a, **kw):
        raise management.ManagementError("нет связи")

    monkeypatch.setattr(health_module.management, "refresh_webhook", failing)
    alerts = FakeAlerts()
    monitor = _monitor(alerts, auto_refresh=True)
    await monitor.apply(_check(webhook_registered=True, webhook_matches=False))
    assert len(alerts.sent) == 1
    assert monitor.state_of(111) == STATE_PROBLEM


async def test_healthy_bot_not_refreshed(refresh_calls):
    alerts = FakeAlerts()
    monitor = _monitor(alerts, auto_refresh=True)
    await monitor.apply(_check(webhook_registered=True, webhook_matches=True))
    assert refresh_calls == [] and alerts.sent == []


async def test_delivery_error_alone_does_not_trigger_refresh(refresh_calls):
    import datetime as dt

    alerts = FakeAlerts()
    monitor = _monitor(alerts, auto_refresh=True)
    await monitor.apply(
        _check(
            webhook_registered=True,
            webhook_matches=True,
            last_error_message="Connection refused",
            last_error_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    assert refresh_calls == [] and len(alerts.sent) == 1
