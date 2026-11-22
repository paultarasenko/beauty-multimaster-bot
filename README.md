# Beauty Multimaster Bot

**English** | [Русский](./README.ru.md)

A Telegram booking bot for beauty professionals. One deployment serves many independent masters: each master has their own bot with their own name, avatar and token, while all of them run on a single application, one PostgreSQL database and one scheduler.

A client picks a service, a date and a time, and books without any back and forth. A master runs the schedule, services, clients and statistics from an admin panel inside their own bot. The service owner has a separate private bot for managing masters.

The bot interface is in Russian. Texts that masters can customize (confirmations, reminders, greeting) are stored per master.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Design decisions](#design-decisions)
- [Tech stack](#tech-stack)
- [Local setup](#local-setup)
- [Configuration](#configuration)
- [Production](#production)
- [Managing masters](#managing-masters)
- [Tests](#tests)
- [Repository layout](#repository-layout)
- [Documentation](#documentation)
- [Security](#security)
- [Limitations](#limitations)
- [License](#license)

## Features

### For clients

- Booking in a few taps: service, date on a calendar (days without free time are marked), time, confirmation. Free slots are calculated from working hours, lunch breaks, blocked time, special days, service duration and the buffer after a service.
- In the "Services and prices" section every service has a "Book" button. It goes straight to date selection for that service and clears the price list from the chat.
- "My bookings": upcoming bookings, rescheduling and cancelling (the master can turn rescheduling off).
- A photo portfolio and reviews (1 to 5 stars, editable).
- "Check time": an image of the chosen day with free and busy time.
- Reminders 24 hours and 2 hours before the visit. A booking can be confirmed, rescheduled or cancelled right from the message.
- A limit on active bookings. When it is reached, the client sends the master a request, and the master accepts or declines it (optionally with a comment). An accepted request allows one extra booking.
- Invite links: a master creates a booking for a person who is not in the bot yet and sends them a `t.me/<bot>?start=inv_<token>` link. Opening it attaches the booking to that person.

### For masters (admin panel inside their own bot)

- Schedule: working hours per weekday, lunch break, exceptions for single dates (open one Saturday, close one day). If confirmed bookings already exist on such a date, the bot warns about it.
- Bookings: lists for a week and for a month, manual booking creation (including clients without Telegram), cancellation with a client notification, blocking and unblocking time.
- Services and prices: create, rename, reprice, set duration and buffer, enable, disable and delete. Past bookings keep their original data.
- Statistics for today, week, month, three and six months: number of bookings, actual and expected revenue, free hours. Plus an Excel report for a chosen period.
- Content: greeting, "About me", "How to find me", portfolio, review moderation.
- Clients and access: after a series of cancellations in a row the master gets a warning and can restrict the client. A restricted client can send one message, and the master decides whether to reopen access or restrict for good.
- Settings: booking step (15, 30 or 60 minutes), which reminders are on, limit of active bookings, cancellation threshold, rescheduling and reviews, currency, custom message texts with placeholders.
- Quiet mode: a daily window in which the bot does not disturb the master. Notifications pile up and are delivered in order when the window ends.
- Personal reminders for the master (once, daily, weekly, monthly, yearly). They are computed in the master's local time, so they do not drift with daylight saving changes.
- "My bot": status, a connection check, webhook re-registration. The token is never shown.

### For the service owner (superadmin bot)

A separate bot that answers only to whitelisted Telegram ids and stays silent for everyone else.

- Create masters, connect and remove their bots, change owners, enable or disable a master. No data is deleted.
- A dashboard with totals (masters, bots, clients, bookings) and no personal data.
- Trial period: the term is only a reminder that a decision is due, nothing is switched off automatically. A daily digest lists masters whose term is ending or has ended.
- Monitoring: alerts when Telegram rejects a bot token, a webhook is missing or points to the wrong address, or delivery errors appear.
- Action journal: who did what, to which master, when, and with what result. Tokens and secrets never get there.
- Reference master: master 1 can be saved as a template, and any demo master can be reset to it in one action (profile, services, schedule, portfolio, texts, bot avatar).

## How it works

```
Telegram
   |  POST /tg/{bot_id}/{secret}      one URL per bot
   v
Caddy (automatic HTTPS)  ->  FastAPI + aiogram 3 (single app process)
                                   |
                  +----------------+-----------------+
                  |                |                 |
              handlers  ->   services  ->  repositories  ->  PostgreSQL
              (Telegram)     (business      (SQLAlchemy 2,     (all data,
                              logic)         always scoped      reminders,
                                             to one master)     constraints)
                  |
                  +-> Redis (dialog state only)
                  +-> APScheduler (reminders for all masters, one scheduler)
```

Business logic lives in `app/services`, not in handlers. A handler receives an update and calls a service, and a service talks to the database through repositories.

**Multi-tenancy.** The master is resolved from the bot that received the update. Every table with master data has a `master_id`, every repository is created for a specific master, and queries are filtered by it. This is held by guard tests (see [Tests](#tests)), not by convention.

**Two run modes** (`RUN_MODE`):

| Mode | What it does | When to use |
| --- | --- | --- |
| `polling` (default) | One bot from `BOT_TOKEN`, one master from `MASTER_ID`, long polling | Development, a single master |
| `webhook` | Many bots: each master has a row in the `master_bots` table, updates arrive over HTTPS | Production, many masters on one service |

## Design decisions

- Double booking is prevented by PostgreSQL itself: a GiST exclusion constraint over `(master_id, time_range)` for confirmed bookings. Overlapping bookings cannot be created even in a race. A test fires two parallel requests at the same slot against a real database.
- Reminders survive restarts. They are rows in the `notifications` table, not in-memory timers. Rows are claimed with `FOR UPDATE SKIP LOCKED`, so parallel workers do not send duplicates.
- A booking stores a snapshot of the service (name, price, duration) as it was at booking time. Changing the price list never rewrites history.
- Bot tokens are stored encrypted (Fernet, `BOT_TOKEN_ENCRYPTION_KEY`) and never reach logs or output. The message with a token is deleted right after it arrives. Webhook URLs contain a per-bot secret, and the `X-Telegram-Bot-Api-Secret-Token` header is checked with a constant-time comparison.
- Dialog state lives in Redis, so unfinished dialogs (booking, review, service input) survive deploys. Keys include the bot id and expire on their own.
- One scheduler for all masters, with failures isolated: a bad token or a flood limit on one bot does not stop reminders for the others. Sending is rate limited per bot. A PostgreSQL advisory lock stops two overlapping processes (for example during a rolling update) from running the same tick.
- Backpressure on the webhook. The number of concurrent updates is capped before a database connection is taken, so one slow bot cannot exhaust the pool. When the queue is full the service answers 503 and Telegram retries later.
- The database stores UTC. The master's time zone (`Europe/Minsk` by default) is used for display and recurrence. It cannot be changed from the bot yet, only in the database.

More detail and the reasoning behind each decision is in [ARCHITECTURE.md](./ARCHITECTURE.md) (in Russian).

## Tech stack

- Python 3.12
- aiogram 3.15
- FastAPI and uvicorn (webhook mode)
- PostgreSQL 16, SQLAlchemy 2 (async, asyncpg), Alembic
- Redis 7 for dialog state
- APScheduler
- Caddy 2 with automatic HTTPS
- pydantic-settings, cryptography (Fernet), Pillow (schedule image), openpyxl (Excel reports)
- Docker and Docker Compose
- pytest and pytest-asyncio, tests run against a real PostgreSQL and Redis

## Local setup

You need Docker with Compose and a bot token from [@BotFather](https://t.me/BotFather).

1. Create the env file and fill it in:

   ```bash
   cp .env.example .env
   ```

   At minimum:

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

3. Open your bot in Telegram, send `/start`, then `/admin` to get into the admin panel.

4. Optionally fill the database with demo data:

   ```bash
   docker compose exec bot python -m app.seed
   ```

`ADMIN_TELEGRAM_ID` only sets the initial owner. At startup it is written to `masters.owner_telegram_id` if that master has no owner yet. After that the owner stored in the database is the source of truth.

### Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# PostgreSQL must be running, the database created and the extension enabled:
psql -c "CREATE EXTENSION IF NOT EXISTS btree_gist;" beauty_bot

cp .env.example .env     # point DATABASE_URL to localhost
alembic upgrade head
python -m app.main
```

## Configuration

All settings come from environment variables. The full list with comments is in [.env.example](./.env.example). The main ones:

| Variable | Purpose |
| --- | --- |
| `RUN_MODE` | `polling` (default) or `webhook` |
| `BOT_TOKEN` | Bot token, polling mode only |
| `ADMIN_TELEGRAM_ID` | Initial owner of master `MASTER_ID` |
| `MASTER_ID` | Which master the polling bot serves |
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `REDIS_URL`, `FSM_STORAGE` | Dialog storage. Default is `memory` for polling and `redis` for webhook |
| `PUBLIC_BASE_URL` | Public https address, required for webhook mode |
| `BOT_TOKEN_ENCRYPTION_KEY` | Fernet key for stored bot tokens, required for webhook mode |
| `SUPERADMIN_BOT_TOKEN`, `SUPERADMIN_TELEGRAM_IDS` | Enable the superadmin bot and set who it answers to |
| `TRIAL_DEFAULT_DAYS`, `SUPERADMIN_DIGEST_HOUR`, `SUPERADMIN_TIMEZONE` | Trial period and the daily digest |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `WEBHOOK_MAX_CONCURRENT_UPDATES` | Connection pool and webhook limits |
| `SCHEDULER_*` | Scheduler concurrency, send rate and timeouts |

Per master settings (booking step, reminders, limits, currency, texts, quiet mode) are not environment variables. Masters change them in the bot under "Settings".

Generate an encryption key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Production

`docker-compose.prod.yml` runs the whole stack: the app in webhook mode, PostgreSQL 16, Redis and Caddy with automatic HTTPS. Only ports 80 and 443 are published, PostgreSQL and Redis are reachable only inside the compose network.

```bash
cp .env.example .env     # fill in the PRODUCTION section
docker compose -f docker-compose.prod.yml up -d --build
```

What you need before the first start:

1. A server with Docker and a domain whose A record points to it. Ports 80 and 443 must be open.
2. In `.env`: `PUBLIC_DOMAIN` (without `https://`), `POSTGRES_PASSWORD` (letters and digits only), `BOT_TOKEN_ENCRYPTION_KEY`, `SUPERADMIN_BOT_TOKEN`, `SUPERADMIN_TELEGRAM_IDS`. Compose refuses to start if a required value is missing.
3. In `deploy/Caddyfile`: your own email for Let's Encrypt notices.

Worth knowing:

- Run exactly one instance of the app. The scheduler is designed for a single process. Do not use `--scale app=2` and do not start a second stack on the same database, or reminders will be duplicated.
- Keep a copy of `.env` and the encryption key off the server. Without the key the stored bot tokens cannot be decrypted and every bot has to be reconnected.
- Health checks: `GET /health` (the process is alive) and `GET /ready` (database and Redis respond). Caddy does not expose `/ready`.
- Backups: `tools/backup/backup.sh` makes a verified compressed dump with 14 day rotation, and `tools/backup/restore.sh` checks that a dump really restores. Scheduling is described in `tools/backup/SCHEDULE.md`.
- Updates and rollback, a migration rehearsal on a fresh dump and the pre-launch checklist are in [docs/OPERATIONS.md](./docs/OPERATIONS.md) (in Russian).

Adding a master is two steps in the superadmin bot: create the master with the owner's Telegram id, then connect the bot token the master got from @BotFather. The service checks the token, stores it encrypted and registers the webhook.

## Managing masters

The superadmin bot is the main interface. The same operations are available from the command line:

```bash
docker compose -f docker-compose.prod.yml exec app python -m app.cli create-master --owner-telegram-id 123456789 [--trial-days 14]
BOT_TOKEN_TO_ADD='<token>' docker compose -f docker-compose.prod.yml exec -e BOT_TOKEN_TO_ADD app python -m app.cli add-bot --master-id 2
docker compose -f docker-compose.prod.yml exec app python -m app.cli list-bots
docker compose -f docker-compose.prod.yml exec app python -m app.cli refresh-webhook --master-id 2 [--rotate-secret]
docker compose -f docker-compose.prod.yml exec app python -m app.cli remove-bot --master-id 2
```

`add-bot` validates the token with `getMe`, stores it encrypted, generates secrets and calls `setWebhook`. Tokens are never printed. One bot belongs to one master and one master has one bot. A running service picks up added and removed bots without a restart.

## Tests

Tests run against a real PostgreSQL and Redis. Only Telegram is mocked.

```bash
docker compose -f docker-compose.test.yml up -d --wait   # PostgreSQL :5434, Redis :6380
pip install -r requirements-dev.txt
pytest tests/ -v                                          # migrations are applied automatically
docker compose -f docker-compose.test.yml down
```

To point the tests elsewhere, set `TEST_DATABASE_URL` (the database name must end with `_test`, its tables are wiped) and `TEST_REDIS_URL`. Tests that need Redis are skipped, with a reason, if it is unavailable.

The suite has over a thousand test functions, about two thousand runs with parametrization. It has been run in full on PostgreSQL 16 and Redis 7, on both dialog state storages. It covers:

- availability calculation (hours, breaks, blocks, long services, day boundaries);
- creating, cancelling and rescheduling bookings, including a real race of parallel requests for one slot;
- reminders: creation, single delivery, behavior after a restart, cancellation together with the booking;
- the scheduler for many masters: routing, failure isolation, no duplicates, hot add and remove of bots, rate limiting;
- webhook handling, token encryption, the superadmin bot, the trial period;
- end to end dialogs on both storages (`memory` and `redis`);
- migrations from scratch and between revisions.

Guard tests fail the build if an admin handler lacks an owner check, if code queries a master table without filtering by `master_id`, if a model is created without `master_id`, or if mutable module level state appears. The static part can be run on its own without a database: `python -m tests.static_guards`.

A load test harness with a fake Telegram and 100 generated masters lives in `tools/loadtest` (see its README).

## Repository layout

```
app/
  bot/            handlers (client, admin), keyboards, middlewares, FSM states
  database/       SQLAlchemy models and repositories
  services/       business logic: availability, bookings, reminders, stats, reports
  scheduler/      a single scheduler for all masters (APScheduler)
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

- [ARCHITECTURE.md](./ARCHITECTURE.md): architectural decisions and the reasoning behind them (in Russian)
- [docs/OPERATIONS.md](./docs/OPERATIONS.md): production runbook, updates, rollback, checklists (in Russian)
- [tools/backup/SCHEDULE.md](./tools/backup/SCHEDULE.md): backup scheduling
- [tools/loadtest/README.md](./tools/loadtest/README.md): load test harness

## Security

- Never commit `.env`. It is in `.gitignore` and `.dockerignore`, and only `.env.example` belongs in git.
- Bot tokens are stored encrypted and are kept out of logs, the action journal and any output. The superadmin bot deletes the message that contains a token.
- The Caddy log is configured to hide webhook secrets in request paths. For the same reason, do not enable the access log.
- Only the owner of a master can open that master's admin panel. The superadmin bot ignores everyone who is not whitelisted.

## Limitations

- One application instance per database. The scheduler and several in-memory structures (webhook admission, per bot send throttling, registry cache, alert deduplication) live inside the process.
- If the process dies exactly while a reminder is being sent, that reminder may be delivered twice.
- The reminder hours (24 and 2) are fixed. Masters can turn each reminder on or off but cannot change the hours.
- The `no_show` booking status is not set automatically.
- The bot interface is in Russian only. There is no payment processing.

## License

Proprietary. All rights reserved. See the [LICENSE](./LICENSE) file.
