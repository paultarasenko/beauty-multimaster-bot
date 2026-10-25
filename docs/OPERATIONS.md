# Эксплуатация (этап 12A)

Рабочий запуск: `docker-compose.prod.yml` (app в webhook-режиме, PostgreSQL 16, Redis, Caddy с автоматическим HTTPS).
Все команды — для Git Bash (или Linux) из папки проекта. Наружу открыты только порты 80 и 443; PostgreSQL и Redis не публикуются.

Сокращение в командах ниже: `P` = `docker compose -f docker-compose.prod.yml`. Задайте его один раз в сессии:

```bash
P() { docker compose -f docker-compose.prod.yml "$@"; }
```

> Правило: **`down -v` и `docker volume rm` не использовать** — тома `bbot-prod_postgres_data` и `bbot-prod_caddy_data` содержат
> данные мастеров и сертификаты.

## 1. Подготовка `.env` (один раз)

```bash
[ -f .env ] || cp .env.example .env; chmod 600 .env   # .env уже есть от старого запуска — оставьте его и добавьте раздел PRODUCTION из .env.example
openssl rand -hex 24                        # → POSTGRES_PASSWORD (только буквы и цифры)
openssl rand -base64 32 | tr '+/' '-_'      # → BOT_TOKEN_ENCRYPTION_KEY (если ключа ещё нет; уже рабочий ключ НЕ менять)
```

Заполните в `.env`: `PUBLIC_DOMAIN`, `POSTGRES_PASSWORD`, `BOT_TOKEN_ENCRYPTION_KEY`, `SUPERADMIN_BOT_TOKEN`, `SUPERADMIN_TELEGRAM_IDS`
(+ `ADMIN_TELEGRAM_ID`, `TIMEZONE`). В `deploy/Caddyfile` замените email `ЗАМЕНИТЬ@example.com` на свой. DNS: A-запись домена → IP сервера.

```bash
grep -n ЗАМЕНИТЬ .env deploy/Caddyfile      # ожидается: только строка email в Caddyfile, пока вы её не замените; в .env — пусто
P config --quiet && echo "compose OK"       # ожидается: compose OK (при незаполненной обязательной переменной — понятная ошибка)
```

## 2. Сохранить ключ и `.env` отдельно от сервера

Без `BOT_TOKEN_ENCRYPTION_KEY` токены ботов в базе **не расшифровать**: данные мастеров целы, но каждого бота придётся подключать заново
(нужны токены от @BotFather). Копия базы ключа не содержит — так задумано.

1. Скопируйте `.env` целиком в менеджер паролей / зашифрованный архив на **другом** устройстве (не рядом с `backups/`).
2. Проверьте, что копия верна — отпечатки совпадают (ключ в консоль не печатается):

```bash
grep '^BOT_TOKEN_ENCRYPTION_KEY=' .env | sha256sum      # сравните с тем же выводом для сохранённой копии
```

3. Меняли `.env` (новые токены, пароль) — обновите копию.

## 3. Перевод рабочего бота со старого кода

Идея: боевой стек живёт **отдельно** (`name: bbot-prod`, свои тома). Старый стек (`docker-compose.yml`) и его база не изменяются —
поэтому откат простой. Рабочий бот мастера №1 подключается **тем же токеном**, что и раньше (иначе `file_id` фото портфолио перестанут открываться).

Команды со старым стеком — с префиксом `COMPOSE_FILE=docker-compose.yml`.

**A. Подготовка (старый бот ещё работает)**

```bash
# 1. Распакуйте новую версию поверх проекта (.env в архиве нет — он не затрётся). Заполните .env по разделу 1.
# 2. Поднимите ТОЛЬКО базу и Redis боевого стека (пустая база; старый бот не затронут):
P up -d postgres redis
P ps                                                     # postgres и redis: Up (healthy)

# 3. Бэкап старой базы:
COMPOSE_FILE=docker-compose.yml bash tools/backup/backup.sh
#    ожидается: «Готово: .../backups/bbot_ДАТА.dump (N байт), оглавление прочитано»

# 4. Проверка, что копия восстанавливается:
bash tools/backup/restore.sh --dump backups/bbot_ДАТА.dump --db restore_check_1
#    ожидается: список таблиц с числом строк, «КОПИЯ ВОССТАНАВЛИВАЕТСЯ», «временная база restore_check_1 удалена»

# 5. Репетиция миграций на этой копии:
bash tools/migration_rehearsal/rehearse.sh --dump backups/bbot_ДАТА.dump --db rehearsal_$(date +%Y%m%d)
#    ожидается: «РЕПЕТИЦИЯ ПРОЙДЕНА». Любой ✗ или «НЕ ПРОЙДЕНА» — дальше НЕ идти, прислать вывод.
```

**B. Переключение (простой — несколько минут; Telegram хранит апдейты и доставит их после запуска)**

```bash
# 6. Остановить старый бот (иначе TelegramConflictError: два потребителя одного токена):
COMPOSE_FILE=docker-compose.yml docker compose stop bot

# 7. Финальный бэкап (бот остановлен — новые записи не потеряются):
COMPOSE_FILE=docker-compose.yml bash tools/backup/backup.sh            # запомните имя файла: FINAL=backups/bbot_ДАТА.dump

# 8. Загрузить финальную копию в боевую базу (она пустая; в непустую база загрузка завершится ошибкой — это защита):
P exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error' < "$FINAL"
P exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM bookings"'
#    число записей должно совпасть со старой базой:
COMPOSE_FILE=docker-compose.yml docker compose exec -T postgres psql -U postgres -d beauty_bot -Atc "SELECT count(*) FROM bookings"

# 9. Запуск (миграции применятся при старте app):
P up -d --build
P ps                                                     # через 1–2 мин: все четыре сервиса Up, app и caddy (healthy)
P logs --tail 50 app                                     # есть «Webhook-сервер: порт 8080», нет ERROR
P logs --tail 20 caddy                                   # «certificate obtained successfully» — HTTPS получен

# 10. Снаружи:
curl -fsS https://ВАШ_ДОМЕН/health                       # ожидается: {"status":"ok"}
```

**C. Подключить бота мастера №1 (webhook)**

Основной способ — суперадмин-бот (`SUPERADMIN_BOT_TOKEN` в `.env`): `👥 Мастера` → мастер №1 → `🤖 Подключить бота` → прислать **прежний токен**
бота мастера (сообщение с токеном бот удалит сам) → `🔍 Проверить подключение`: токен принят, webhook указывает на ваш домен, ошибок нет.

Запасной способ — CLI (токен берётся из старого `.env`, в консоль не печатается):

```bash
P exec -e BOT_TOKEN_TO_ADD="$(grep '^BOT_TOKEN=' .env | cut -d= -f2-)" app python -m app.cli add-bot --master-id 1
P exec app python -m app.cli list-bots                   # ожидается: мастер 1, бот @..., активен
```

(`BOT_TOKEN` в `.env` остался от старого запуска; после успешной проверки удалите эту строку — в webhook-режиме она не нужна.)

**D. Проверить webhook и работу**

```bash
read -rsp "Токен бота мастера: " T; echo
curl -s "https://api.telegram.org/bot$T/getWebhookInfo" | grep -oE '"(pending_update_count|last_error_message)":[^,}]*'; unset T
#    ожидается: pending_update_count → 0 за минуту-две; last_error_message отсутствует
```

Затем с телефона мастера: `/start`, открыть админ-панель, сделать тестовую запись другим аккаунтом, проверить уведомление мастеру.
Старый стек и его том **не удалять минимум неделю** (откат). Остановленный старый стек не мешает: `COMPOSE_FILE=docker-compose.yml docker compose stop`.

**Откат (если после запуска что-то не так)**

```bash
P exec app python -m app.cli remove-bot --master-id 1     # снимает webhook (иначе у старого бота polling не заработает)
P stop app caddy                                          # без -v!
COMPOSE_FILE=docker-compose.yml docker compose start bot  # именно start (старый контейнер, старый образ), не up --build
```

Данные в старой базе — на момент шага 7. Записи, сделанные клиентами после запуска, остались только в новой базе
(сохраните их: `bash tools/backup/backup.sh`, копия лежит в `backups/`); в старую базу автоматически они не переносятся.

## 4. Обновление версии и откат

```bash
bash tools/backup/backup.sh                                                       # 1. бэкап
bash tools/migration_rehearsal/rehearse.sh --dump "$(ls -t backups/bbot_*.dump | head -1)" --db rehearsal_$(date +%Y%m%d)   # 2. репетиция
# 3. распаковать новую версию поверх проекта
P up -d --build                                                                   # 4. миграции применяются при старте app
P ps; P logs --tail 50 app; curl -fsS https://ВАШ_ДОМЕН/health                   # 5. проверка
```

**Откат кода без новых миграций:** вернуть прежние файлы проекта и `P up -d --build`.

**Откат с миграциями.** Порядок важен: сначала откатить схему **новым** образом (в нём есть файл миграции), потом вернуть старый код.

```bash
P stop app
P run --rm --no-deps app alembic downgrade -1             # на 1 шаг; текущая версия: P run --rm --no-deps app alembic current
# вернуть прежние файлы проекта
P up -d --build
```

`downgrade` **безопасен**, если миграция только добавила таблицы/колонки/индексы, а данных в них ещё нет (репетиция показывает это: после
цикла downgrade/upgrade число строк не меняется). **Небезопасен**, если миграция переносила, преобразовывала или удаляла данные
(например, переносила флаги клиентов, удаляла таблицу или колонку): после отката данные не вернутся. Тогда — восстановление из бэкапа,
сделанного перед обновлением (потеряются записи, появившиеся после него):

```bash
P stop app
P exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE \"$POSTGRES_DB\" WITH (FORCE)" -c "CREATE DATABASE \"$POSTGRES_DB\""'
P exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error' < backups/bbot_ДАТА.dump
# вернуть прежние файлы проекта
P up -d --build
```

Перед такой операцией сделайте ещё один бэкап текущего состояния.

## 5. Всё ли живо

```bash
P ps                                                       # все сервисы Up; у postgres, redis, app, caddy — (healthy)
curl -fsS https://ВАШ_ДОМЕН/health                         # {"status":"ok"}: процесс жив (снаружи доступен только /health)
P exec -T app python - <<'PY'                              # /ready: база и Redis; внутри контейнера
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen("http://127.0.0.1:8080/ready", timeout=4).read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
PY
#    ожидается: {"status":"ready",...}; «503 ... not_ready» — недоступна база или Redis
P logs --tail 100 app                                      # свежие логи (последний запрос — P logs -f app)
P logs --since 1h app 2>&1 | grep -E "ERROR|CRITICAL"     # ошибки за час; пусто — хорошо
P logs --tail 30 caddy                                     # сертификат, ошибки проксирования
ls -lh backups | tail -5                                   # свежие копии есть
docker system df                                           # место под образы и тома; df -h — свободное место на диске
```

В суперадмин-боте: `📊 Сводка` (мастера, боты активные/неактивные, клиенты, записи), `👥 Мастера` (состояние webhook каждого бота),
`📜 Журнал` (последние действия). Сигналы о проблемах (отвергнут токен, webhook не отвечает) и ежедневная сводка о сроках приходят сами.
Рекомендуется внешний монитор (например, UptimeRobot) на `https://ВАШ_ДОМЕН/health`: он оповестит, если сервер недоступен целиком.

## 6. Чек-лист подключения нового мастера

1. Мастер создаёт **своего** бота у @BotFather (`/newbot`) и присылает токен и свой Telegram id (@userinfobot). Токен — только в личном сообщении.
2. Суперадмин-бот: `➕ Создать мастера` → id владельца → срок пробного периода (или `Без срока`).
3. Карточка мастера → `🤖 Подключить бота` → токен (сообщение с токеном бот удалит; если не удалил — удалите вручную).
4. `🔍 Проверить подключение`: токен принят, webhook на ваш домен, ошибок доставки нет.
5. Мастер пишет своему боту `/start`, открывает админ-панель, задаёт услуги, расписание, часовой пояс.
6. Тестовая запись клиентским аккаунтом: мастер получил уведомление, запись видна в расписании; затем отмена тестовой записи.
7. `📜 Журнал` показывает создание мастера и подключение бота; срок пробного периода записан в карточке.

## 7. Чек-лист «перед запуском для настоящих мастеров»

- [ ] `https://ВАШ_ДОМЕН/health` отвечает снаружи, сертификат действителен; `grep -n ЗАМЕНИТЬ .env deploy/Caddyfile` — пусто.
- [ ] С другого компьютера порты 5432, 6379 и 8080 сервера недоступны (открыты только 80 и 443).
- [ ] `.env`: нет значений по умолчанию, `chmod 600`; **копия `.env` и ключа лежит отдельно от сервера**, отпечаток ключа сверен (раздел 2).
- [ ] `backup.sh` выполнен вручную и стоит в расписании (`tools/backup/SCHEDULE.md`); последняя копия проверена `restore.sh`; копии выносятся с сервера.
- [ ] Репетиция миграций пройдена на свежем дампе («РЕПЕТИЦИЯ ПРОЙДЕНА»).
- [ ] Суперадмин-бот отвечает только вам; `📊 Сводка` открывается; сигнал о проблеме приходит (например, временно выключите бота мастера кнопкой и включите обратно).
- [ ] Перезапуск проверен: `P restart app` и перезагрузка сервера — сервисы поднялись сами (`restart: unless-stopped`; Docker должен запускаться вместе с системой), бот отвечает.
- [ ] Бот мастера №1: webhook без ошибок, напоминания приходят вовремя, тестовая запись проходит.
- [ ] Свободного места на диске достаточно для базы и копий (`df -h`); логи ограничены автоматически (10 МБ × 5 файлов на контейнер).
- [ ] Нагрузка с ~100 мастерами **ещё не проверена** (этап 12B): до неё подключайте мастеров партиями и смотрите `📊 Сводку` и логи.
