# Beauty Multimaster Bot

**English** | [Русский](./README.ru.md)

A Telegram booking bot for beauty professionals, built to serve many independent masters from a single deployment. Clients pick a service, a date and a time, and book without any back and forth. Masters run their whole business (schedule, services, clients, statistics) from an admin panel inside their own bot.

Every master gets **their own Telegram bot** with their own name, avatar and token, while all of them are served by one application, one PostgreSQL database and one scheduler. The service owner manages masters through a separate, private superadmin bot.

The bot has no AI or LLM in it. All behavior comes from deterministic business logic and the database.

> The interface language of the bot is Russian. Texts that masters can customize (confirmations, reminders, greeting) are stored per master.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Tech stack](#tech-stack)
- [Quick start (local)](#quick-start-local)
- [Configuration](#configuration)
- [Production deployment](#production-deployment)
- [Managing masters](#managing-masters)
- [Testing](#testing)
- [Repository layout](#repository-layout)
- [Documentation](#documentation)
- [Security notes](#security-notes)
- [Known limitations](#known-limitations)
- [License](#license)

## Features

### For clients

- **Booking in a few taps:** service, date on a calendar (days without free time are marked), time slot, confirmation. Slot availability accounts for working hours, lunch breaks, blocked time, special days, service duration and an optional buffer after a service.
- **My bookings:** view upcoming bookings, reschedule or cancel (the master can turn rescheduling off).
- **Services and prices, portfolio:** a catalog with prices and durations, and a photo portfolio you can flip through.
- **Check time:** an image of a chosen day showing busy and free time.
- **Info section:** about the master, how to find them, reviews (1 to 5 stars, with editing), a way to contact the master directly.
- **Reminders:** 24 hours and 2 hours before the visit, with buttons to confirm ("all good"), reschedule or cancel right from the message.
- **Booking limits with a human fallback:** when a client reaches the limit of active bookings, they can send the master a request. The master accepts or declines it, optionally with a message, and an accepted request allows one extra booking.
- **Invite links:** a master can create a booking for a person who is not in the bot yet and send a `t.me/<bot>?start=inv_<token>` link. Opening it attaches the booking to that person.

### For masters (admin panel inside their own bot)

- **Schedule:** weekly working hours picked with inline buttons, lunch break, and per-date exceptions (open a single Saturday, close a single day, with a warning when confirmed bookings already exist on that date).
- **Bookings:** lists for a week, a month and three months, manual booking creation (including clients without Telegram), cancellation with a client notification, blocking and unblocking time.
- **Services and prices:** create, rename, reprice, set duration and buffer time, enable, disable and delete services. Past bookings keep their original data.
- **Statistics:** today, week, month and three months. Number of bookings, actual revenue, expected revenue and free hours. Plus an **Excel report** for a chosen period (one row per booking, visits and cancellations per client, income totals).
- **Content:** greeting, "About me", "How to find me", portfolio with photos, review moderation.
- **Client profile and access control:** after a series of cancellations in a row the master gets a warning and can restrict that client. A restricted client can send one message, and the master decides to reopen access or restrict permanently.
- **Settings per master:** booking step (15, 30 or 60 minutes), which reminders are on, limit of active bookings, cancellation threshold, rescheduling and reviews on or off, currency, custom message texts with placeholders.
- **Quiet mode:** a daily time window in which the bot does not notify the master. Notifications are stored and delivered in order when the window ends.
- **Personal reminders:** recurring reminders for the master themselves (once, daily, weekly, monthly, yearly), computed in the master's local time so they do not drift with daylight saving changes.
- **My bot:** bot status, a connection check and webhook re-registration. The token is never shown.

### For the service owner (superadmin bot)

A separate bot that answers only to whitelisted Telegram ids and stays silent for everyone else.

- Create masters, connect and remove their bots, change owners, enable or disable a master (data is never deleted).
- Dashboard with totals (masters, bots, clients, bookings) and no personal data.
- Trial period tracking: the term is only a reminder to make a decision, nothing is switched off automatically. A daily digest lists masters whose term ends or has ended.
- Health monitoring: alerts when Telegram rejects a bot token, a webhook is missing or points to the wrong address, or delivery errors appear.
- Action journal: who did what, to which master, when, and with what result. Tokens and secrets never get there.
- Reference master: save master 1 as a template and reset any demo master to it in one action (profile, services, schedule, portfolio, texts, bot avatar).

## Architecture

```
Telegram
   |  POST /tg/{bot_id}/{secret}      one URL per master bot
   v
Caddy (automatic HTTPS)  ->  FastAPI + aiogram 3 (single app process)
                                   |
                  +----------------+-----------------+
                  |                |                 |
              handlers  ->   services  ->  repositories  ->  PostgreSQL
              (Telegram)     (business      (SQLAlchemy 2,     (all data,
                              logic)         always scoped      reminders,
                                             to a master)       constraints)
                  |
                  +-> Redis (FSM dialog state only)
                  +-> APScheduler (reminders for all masters, one scheduler)
```

Business logic lives in `app/services`, not in handlers. A handler receives an update and calls a service. A service talks to the database through repositories.

**Multi-tenancy.** The master is resolved from the bot that received the update. Every table that holds master data has a `master_id`, every repository is created for a specific master, and queries are filtered by it. This is enforced by guard tests (see [Testing](#testing)), not only by convention.

**Two run modes** (`RUN_MODE`):

| Mode | What it does | When to use |
| --- | --- | --- |
| `polling` (default) | One bot from `BOT_TOKEN`, one master from `MASTER_ID`, long polling | Local development, single master |
| `webhook` | Multi-bot: each master has a bot in the `master_bots` table, updates arrive over HTTPS | Production, many masters on one service |

## Design decisions

- **Double booking is prevented by PostgreSQL itself.** A GiST exclusion constraint over `(master_id, time_range)` for confirmed bookings makes overlapping bookings impossible even under a race. There is a test that fires two parallel requests at the same slot against a real database.
- **Reminders survive restarts.** They are rows in the `notifications` table, not timers in memory. Rows are claimed with `FOR UPDATE SKIP LOCKED`, so parallel workers do not send duplicates.
- **Booking snapshots.** A booking stores the service name, price and duration as they were at booking time, so changing the price list never rewrites history.
- **Bot tokens are encrypted at rest** with Fernet (`BOT_TOKEN_ENCRYPTION_KEY`) and are never logged, printed or shown. The message with a token is deleted right after it is received. Webhook URLs contain a per-bot secret, and the `X-Telegram-Bot-Api-Secret-Token` header is verified with a constant-time comparison.
- **Dialog state in Redis.** Unfinished dialogs (booking, review, service input) survive deploys and restarts. Keys are namespaced by bot id and expire on their own.
- **One scheduler for every master.** Failures are isolated per master and per bot: a bad token or a flood limit on one bot does not stop reminders for the others. Sending is rate limited per bot. A PostgreSQL advisory lock stops two overlapping processes (for example during a rolling update) from running the same tick.
- **Backpressure on the webhook.** The number of concurrent updates is capped before a DB connection is taken, so one slow bot cannot exhaust the connection pool. When the queue is full the service answers 503 and Telegram retries.
- **Time zones per master.** The database stores UTC. The master's time zone (stored in the master profile, `Europe/Minsk` by default) is used for display and recurrence. There is no in-bot control for it yet, it is changed in the database.

More detail and the reasoning behind each decision is in [ARCHITECTURE.md](./ARCHITECTURE.md) (in Russian).

## Tech stack

| Area | Technology |
| --- | --- |
| Language | Python 3.12 |
| Telegram | aiogram 3.15 |
| Web server | FastAPI, uvicorn (webhook mode) |
| Database | PostgreSQL 16, SQLAlchemy 2 (async, asyncpg), Alembic |
| Dialog state | Redis 7 |
| Scheduling | APScheduler |
| Reverse proxy | Caddy 2 (automatic HTTPS) |
| Other | pydantic-settings, cryptography (Fernet), Pillow (schedule image), openpyxl (Excel reports) |
| Packaging | Docker, Docker Compose |
| Tests | pytest, pytest-asyncio, a real PostgreSQL and Redis |

## Quick start (local)

Requirements: Docker with Compose, and a bot token from [@BotFather](https://t.me/BotFather).

1. Create the env file and fill it in:

   ```bash
   cp .env.example .env
   ```

   At minimum set:

   ```env
   BOT_TOKEN=            # from @BotFather (/newbot)
   ADMIN_TELEGRAM_ID=    # your numeric Telegram id, for example from @userinfobot
   MASTER_ID=1
   TIMEZONE=Europe/Minsk
   ```

2. Start the stack (PostgreSQL, Redis, the bot). Migrations are applied automatically on start.

   ```bash
   docker compose up --build
   ```

3. Open your bot in Telegram, send `/start`, then `/admin` to open the admin panel.

4. Optional: fill the database with demo data.

   ```bash
   docker compose exec bot python -m app.seed
   ```

`ADMIN_TELEGRAM_ID` is only the initial owner. At startup it is written to `masters.owner_telegram_id` if that master has no owner yet. After that the owner stored in the database is the source of truth.

### Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# PostgreSQL must be running and the database created, with the extension enabled:
psql -c "CREATE EXTENSION IF NOT EXISTS btree_gist;" beauty_bot

cp .env.example .env     # point DATABASE_URL to localhost
alembic upgrade head
python -m app.main
```

## Configuration

All settings come from environment variables (see [.env.example](./.env.example), which is the complete reference). The most important ones:

| Variable | Purpose |
| --- | --- |
| `RUN_MODE` | `polling` (default) or `webhook` |
| `BOT_TOKEN` | Bot token, used in polling mode only |
| `ADMIN_TELEGRAM_ID` | Initial owner of master `MASTER_ID` |
| `MASTER_ID` | Which master the polling bot serves |
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `REDIS_URL`, `FSM_STORAGE` | Dialog storage. Default is `memory` for polling and `redis` for webhook |
| `PUBLIC_BASE_URL` | Public https address, required for webhook mode |
| `BOT_TOKEN_ENCRYPTION_KEY` | Fernet key for stored bot tokens, required for webhook mode |
| `SUPERADMIN_BOT_TOKEN`, `SUPERADMIN_TELEGRAM_IDS` | Enable the superadmin bot and set who can use it |
| `TRIAL_DEFAULT_DAYS`, `SUPERADMIN_DIGEST_HOUR`, `SUPERADMIN_TIMEZONE` | Trial period and the daily digest |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `WEBHOOK_MAX_CONCURRENT_UPDATES` | Connection pool and webhook limits |
| `SCHEDULER_*` | Scheduler concurrency, rate limit and timeouts |

Per master settings (booking step, reminders, limits, currency, texts, quiet mode) are not environment variables. Masters change them in the bot under `Settings`.

Generate an encryption key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Production deployment

`docker-compose.prod.yml` runs the full stack: the app in webhook mode, PostgreSQL 16, Redis and Caddy with automatic HTTPS. Only ports 80 and 443 are published. PostgreSQL and Redis are reachable only inside the compose network.

```bash
cp .env.example .env     # fill in the PRODUCTION section
docker compose -f docker-compose.prod.yml up -d --build
```

What you need before the first start:

1. A server with Docker and a domain whose DNS `A` record points to it. Ports 80 and 443 must be open.
2. In `.env`: `PUBLIC_DOMAIN` (without `https://`), `POSTGRES_PASSWORD` (letters and digits only), `BOT_TOKEN_ENCRYPTION_KEY`, `SUPERADMIN_BOT_TOKEN`, `SUPERADMIN_TELEGRAM_IDS`. Compose refuses to start if a required value is missing.
3. In `deploy/Caddyfile`: your own email for Let's Encrypt notices.

Things worth knowing:

- **Run exactly one instance of the app.** The scheduler is designed for a single process. Do not use `--scale app=2` and do not start a second stack on the same database, or reminders will be duplicated.
- **Keep a copy of `.env` and the encryption key off the server.** Without the key the stored bot tokens cannot be decrypted and every bot has to be reconnected.
- **Health checks:** `GET /health` (process is alive) and `GET /ready` (database and Redis respond). `/ready` is not exposed through Caddy.
- **Backups:** `tools/backup/backup.sh` creates a verified compressed dump with 14 day rotation, `tools/backup/restore.sh` checks that a dump actually restores. A cron and Task Scheduler setup is described in `tools/backup/SCHEDULE.md`.
- **Updates and rollback**, migration rehearsal on a fresh dump, and the pre-launch checklist are in [docs/OPERATIONS.md](./docs/OPERATIONS.md) (in Russian).

Adding a master is a two step job in the superadmin bot: create the master with the owner's Telegram id, then connect the bot token the master got from @BotFather. The service checks the token, stores it encrypted and registers the webhook.

## Managing masters

The superadmin bot is the primary interface. The same operations are available from the command line:

```bash
docker compose -f docker-compose.prod.yml exec app python -m app.cli create-master --owner-telegram-id 123456789 [--trial-days 14]
BOT_TOKEN_TO_ADD='<token>' docker compose -f docker-compose.prod.yml exec -e BOT_TOKEN_TO_ADD app python -m app.cli add-bot --master-id 2
docker compose -f docker-compose.prod.yml exec app python -m app.cli list-bots
docker compose -f docker-compose.prod.yml exec app python -m app.cli refresh-webhook --master-id 2 [--rotate-secret]
docker compose -f docker-compose.prod.yml exec app python -m app.cli remove-bot --master-id 2
```

`add-bot` validates the token with `getMe`, stores it encrypted, generates secrets and calls `setWebhook`. Tokens are never printed. One bot belongs to one master and one master has one bot. A running service picks up added and removed bots without a restart.

## Testing

Tests run against a real PostgreSQL and Redis, not mocks of them. Telegram is mocked.

```bash
docker compose -f docker-compose.test.yml up -d --wait   # PostgreSQL :5434, Redis :6380
pip install -r requirements-dev.txt
pytest tests/ -v                                          # migrations are applied automatically
docker compose -f docker-compose.test.yml down
```

Use `TEST_DATABASE_URL` (the database name must end with `_test`, tables are wiped) and `TEST_REDIS_URL` to point the tests elsewhere. Tests that need Redis are skipped, with a reason, if it is unavailable.

The suite has over 1000 test functions. Highlights:

- availability calculation (hours, breaks, blocks, long services, day boundaries)
- creating, cancelling and rescheduling bookings, and a **real race** of parallel requests for one slot
- reminders: creation, single delivery, behavior after a restart, cancellation with the booking
- the scheduler for many masters: routing, failure isolation, no duplicates, hot add and remove of bots, rate limiting
- webhook handling, token encryption, the superadmin bot, the trial period
- end to end dialogs on both FSM storages (`memory` and `redis`)
- migrations from scratch and between revisions

**Guard tests** fail the build if an admin handler lacks an owner check, if code queries a master table without filtering by `master_id`, if a model is created without `master_id`, or if mutable module level state appears. The static part can be run on its own without a database: `python -m tests.static_guards`.

A load test harness with a fake Telegram and 100 generated masters lives in `tools/loadtest` (see its README).

## Repository layout

```
app/
  bot/            handlers (client, admin), keyboards, middlewares, FSM states
  database/       SQLAlchemy models and repositories
  services/       business logic: availability, bookings, reminders, stats, reports
  scheduler/      single scheduler for all masters (APScheduler)
  multibot/       webhook server, bot registry, token encryption, management
  superadmin/     the owner's bot: masters, trial periods, health alerts, digest
  tenancy/        resolving the master for an update
  reference/      reference master snapshot and reset
  config/         pydantic-settings
  cli.py          command line management
  main.py         entry point
alembic/          database migrations
deploy/           Caddy configuration
docs/             operations guide
tests/            pytest suite
tools/            backups, migration rehearsal, load test harness
```

## Documentation

- [ARCHITECTURE.md](./ARCHITECTURE.md): architectural decisions and the reasoning behind them (Russian)
- [docs/OPERATIONS.md](./docs/OPERATIONS.md): production runbook, updates, rollback, checklists (Russian)
- [tools/backup/SCHEDULE.md](./tools/backup/SCHEDULE.md): backup scheduling
- [tools/loadtest/README.md](./tools/loadtest/README.md): load test harness

## Security notes

- Never commit `.env`. It is in `.gitignore` and `.dockerignore`; only `.env.example` belongs in git.
- Bot tokens are stored encrypted and are excluded from logs, the action journal and any output. The superadmin bot deletes the message that contains a token.
- The Caddy log is configured to hide webhook secrets in request paths. Do not enable the access log for the same reason.
- Only the owner of a master can open that master's admin panel. The superadmin bot ignores everyone who is not whitelisted.

## Known limitations

- One application instance per database. The scheduler and several in memory structures (webhook admission, per bot send throttling, registry cache, alert deduplication) are per process.
- If the process dies exactly while a reminder is being sent, that reminder may be delivered twice.
- The reminder hours (24 and 2) are fixed. Masters can turn each reminder on or off but not change the hours.
- The `no_show` booking status is not set automatically.
- The bot interface is in Russian only. There is no payment processing.

## License

No license has been chosen yet. Until a license file is added, all rights are reserved by the author.
