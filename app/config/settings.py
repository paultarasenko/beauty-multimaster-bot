from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Вся конфигурация приложения читается из .env / переменных окружения."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/beauty_bot"
    # Только начальная загрузка владельца мастера MASTER_ID (см. app/tenancy/owner.py: bootstrap_owner). Права определяются
    # masters.owner_telegram_id, а не этим значением.
    admin_telegram_id: int = 0
    # Какой мастер обслуживается этим процессом (пока бот один; на этапе 4 мастер определяется по боту/webhook).
    master_id: int = 1
    timezone: str = "Europe/Minsk"
    # УСТАРЕЛО с этапа 8 и больше НЕ используется: шаг сетки записи — настройка мастера (master_settings.slot_interval_minutes),
    # часы напоминаний фиксированы (24 и 2). Поля оставлены, чтобы старые .env не ломали запуск.
    slot_interval_minutes: int = 30
    reminder_hours_before: int = 24
    reminder_poll_interval_seconds: int = 60

    # Единый планировщик всех мастеров (этап 6).
    # Сколько мастеров обрабатывается одновременно (общий лимит на все задачи; каждый мастер держит до одного соединения с БД).
    scheduler_max_concurrent_tenants: int = 5
    # Лимит отправки на ОДНОГО бота (сообщений в секунду; Telegram допускает порядка 30).
    scheduler_bot_rate_per_second: float = 25.0
    # Таймаут одной отправки в Telegram, секунд.
    scheduler_send_timeout_seconds: float = 15.0
    # Предельное время обработки одного мастера за один тик, секунд (страховка от «зависшего» мастера).
    scheduler_tenant_timeout_seconds: float = 120.0
    # Сколько уведомлений мастера забирается за один тик (остальные — на следующем).
    scheduler_notification_batch_size: int = 100
    # Через сколько секунд уведомление, застрявшее в статусе sending (процесс упал посреди отправки), возвращается в очередь.
    # Должно заметно превышать scheduler_tenant_timeout_seconds, иначе медленная отправка продублируется.
    scheduler_claim_stale_seconds: int = 600
    # Сколько секунд ждём завершения текущих отправок при остановке приложения.
    scheduler_shutdown_timeout_seconds: float = 30.0

    # Режим запуска (python -m app.main): "polling" — один бот из BOT_TOKEN и мастер из MASTER_ID (локальная разработка,
    # обратная совместимость); "webhook" — мульти-бот: у каждого мастера свой бот в таблице master_bots, апдейты по webhook.
    run_mode: Literal["polling", "webhook"] = "polling"
    # Только для RUN_MODE=webhook и CLI (проверяются при старте режима, а не при импорте — polling работает без них).
    public_base_url: str = ""  # https://bot.example.com — по нему CLI регистрирует webhook у Telegram
    bot_token_encryption_key: str = ""  # ключ Fernet для master_bots.bot_token_encrypted
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_max_body_bytes: int = 1_048_576  # апдейты Telegram намного меньше; больше — 413
    # Адрес Bot API (этап 12B): пусто — настоящий https://api.telegram.org. Задаётся ТОЛЬКО в нагрузочном стенде
    # (tools/loadtest), где вместо Telegram отвечает поддельный сервер; токены уходят на этот адрес, поэтому в рабочем .env пусто.
    telegram_api_base: str = ""

    # Этап 12C: пул соединений БД. Расчёт «пул ≤ max_connections PostgreSQL с запасом» — в README (раздел про пул).
    # Было (умолчания SQLAlchemy) 5 + 10 = 15: на стенде все 15 соединений стояли «idle in transaction», а задержки упирались
    # в pool_timeout. Теперь 10 + 15 = 25; всё, что выше — ждёт в очереди webhook (см. webhook_max_concurrent_updates), а не в пуле.
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=15, ge=0, le=200)
    # Сколько секунд ждать свободное соединение из пула (потом ошибка). Должно быть заметно больше webhook_queue_timeout_seconds.
    db_pool_timeout: float = Field(default=30.0, gt=0)
    # Через сколько секунд пересоздавать соединение (защита от обрывов на стороне сети/PostgreSQL); -1 — не пересоздавать.
    db_pool_recycle: int = Field(default=1800, ge=-1)

    # Этап 12C: защита webhook от перегрузки. Апдейт сначала получает «место» (без соединения с БД), потом открывает сессию.
    # Всего одновременно обрабатываемых апдейтов; должно быть меньше пула БД (запас — планировщику и сервисным запросам).
    webhook_max_concurrent_updates: int = Field(default=16, ge=1, le=500)
    # Одновременно обрабатываемых апдейтов ОДНОГО бота: медленный бот не может занять все места и весь пул.
    webhook_bot_max_concurrency: int = Field(default=4, ge=1, le=500)
    # Сколько секунд апдейт ждёт своё место; потом 503 — Telegram повторит доставку позже (апдейт не теряется).
    webhook_queue_timeout_seconds: float = Field(default=20.0, gt=0)
    # Таймаут запроса к Bot API, секунд (у aiogram по умолчанию 60). Скачивание/отправка файлов укладывается с запасом.
    telegram_request_timeout_seconds: float = Field(default=20.0, gt=0)

    # Суперадмин-бот владельца сервиса (этап 9). Пустой SUPERADMIN_BOT_TOKEN — функция выключена, остальное работает как раньше.
    # SUPERADMIN_TELEGRAM_IDS — Telegram id через запятую: только им бот отвечает, остальным — молчание.
    superadmin_bot_token: str = ""
    superadmin_telegram_ids: str = ""
    # Не чаще одного уведомления суперадмину об одном и том же событии (недействительный токен, webhook не отвечает) за такой период, секунд.
    superadmin_alert_interval_seconds: int = Field(default=3600, gt=0)
    # Как часто (RUN_MODE=webhook) проверяются боты мастеров через getMe/getWebhookInfo, секунд.
    superadmin_health_interval_seconds: int = Field(default=600, gt=0)
    # Если проверка видит, что webhook не установлен или указывает на другой адрес, он переустанавливается автоматически
    # (то же, что кнопка «Перерегистрировать webhook»). По умолчанию выключено.
    superadmin_webhook_auto_refresh: bool = False
    # Пробный период (этап 10). Длина по умолчанию (дней) только ПРЕДЛАГАЕТСЯ суперадмину при создании мастера и никогда не
    # применяется сама; пусто — «без срока». Ничего не отключается автоматически: срок лишь напоминает принять решение.
    trial_default_days: int | None = Field(default=None, gt=0, le=3650)
    # Ежедневная сводка суперадминам о сроках: час отправки (0–23) и часовой пояс; пустой пояс — как в проекте (TIMEZONE).
    superadmin_digest_hour: int = Field(default=10, ge=0, le=23)
    superadmin_timezone: str = ""

    # FSM-хранилище диалогов (этап 7). Redis нужен только для FSM (не кэш и не очередь).
    redis_url: str = "redis://localhost:6379/0"  # может содержать пароль: нигде не логируется
    # "memory" | "redis"; пусто — по режиму: webhook → redis, polling → memory. Явный memory в webhook допустим (с предупреждением в логе).
    fsm_storage: Literal["memory", "redis"] | None = None
    # Сколько секунд живут состояние и данные диалога без обновлений (потом Redis удаляет ключи сам). По умолчанию 3 суток.
    fsm_state_ttl_seconds: int = Field(default=3 * 24 * 3600, gt=0)

    @field_validator("telegram_api_base")
    @classmethod
    def _valid_telegram_api_base(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("TELEGRAM_API_BASE: адрес должен начинаться с http:// или https:// (пример: http://fake-telegram:8081)")
        return value

    @field_validator("trial_default_days", mode="before")
    @classmethod
    def _empty_trial_days_means_no_term(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return int(value) if value else None
        return value

    @field_validator("superadmin_timezone")
    @classmethod
    def _valid_superadmin_timezone(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                ZoneInfo(value)
            except Exception:
                raise ValueError(f"SUPERADMIN_TIMEZONE: неизвестный часовой пояс «{value}» (пример: Europe/Minsk)") from None
        return value

    @field_validator("fsm_storage", mode="before")
    @classmethod
    def _empty_fsm_storage_means_auto(cls, value):
        if isinstance(value, str):
            value = value.strip().lower()
            return value or None
        return value


settings = Settings()