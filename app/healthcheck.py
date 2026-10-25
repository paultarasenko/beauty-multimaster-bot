"""Проверка для Docker HEALTHCHECK: `python -m app.healthcheck`.

RUN_MODE=webhook — GET /ready на локальном порту (БД и, если FSM в Redis, Redis); код возврата 0 — готов, 1 — нет.
RUN_MODE=polling — HTTP-сервера нет, проверять нечего: всегда 0 (healthcheck не должен ломать запуск по умолчанию).
"""

import sys
import urllib.error
import urllib.request

from app.config.settings import settings

TIMEOUT_SECONDS = 4


def check() -> bool:
    if settings.run_mode != "webhook":
        return True
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{settings.webhook_port}/ready", timeout=TIMEOUT_SECONDS) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):  # HTTPError (503) — подкласс URLError
        return False


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
