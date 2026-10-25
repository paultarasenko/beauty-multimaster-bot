#!/usr/bin/env bash
# Общие функции для tools/backup/*.sh и tools/migration_rehearsal/rehearse.sh. Подключается через `source`, сам не запускается.
# Все операции с PostgreSQL выполняются ВНУТРИ контейнера postgres (docker compose exec): на хосте не нужны psql/pg_dump,
# а логин и пароль берутся из переменных самого контейнера (POSTGRES_USER, POSTGRES_DB) — в консоль они не попадают.

# Git Bash (MSYS) иначе портит аргументы, начинающиеся с «/».
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# Каталог, откуда запустили скрипт: относительные пути в аргументах (--dump ...) считаются от него.
CALLER_PWD="$PWD"
abs_path() {
  case "$1" in
    /*|[A-Za-z]:[\\/]*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$CALLER_PWD" "$1" ;;
  esac
}

# Корень проекта = два уровня выше tools/<папка>/скрипт.sh
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

# Какой compose-файл использовать. Для старого стека: COMPOSE_FILE=docker-compose.yml
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { printf '[%s] ОШИБКА: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; exit 1; }

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

require_docker() {
  command -v docker >/dev/null 2>&1 || die "команда docker не найдена"
  docker compose version >/dev/null 2>&1 || die "нужен Docker Compose v2 (команда «docker compose»)"
  [ -f "$COMPOSE_FILE" ] || die "нет файла $COMPOSE_FILE (запускайте из проекта или задайте COMPOSE_FILE)"
}

require_postgres_running() {
  local id
  id="$(compose ps -q --status running postgres 2>/dev/null)" || id=""
  [ -n "$id" ] || die "контейнер postgres не запущен. Запустите: docker compose -f $COMPOSE_FILE up -d postgres"
}

# Допустимое имя временной БД: заданный префикс + [a-z0-9_]. Строгий шаблон исключает кавычки и SQL-инъекции в имени.
# Использование: validate_temp_db_name <имя> <префикс>
validate_temp_db_name() {
  local name="$1" prefix="$2"
  [[ "$name" =~ ^${prefix}[a-z0-9_]{1,40}$ ]] || die "имя временной базы должно начинаться с «${prefix}» и содержать только a-z, 0-9, _ (до 40 знаков после префикса); получено: «$name»"
  local work_db
  work_db="$(compose exec -T postgres sh -c 'printf %s "$POSTGRES_DB"' 2>/dev/null)" || work_db=""
  [ "$name" != "$work_db" ] || die "имя временной базы совпадает с рабочей базой — отказ"
}

# psql внутри контейнера, базу задаёт первый аргумент; SQL — через stdin. Вывод без рамок: поля через «|».
psql_db() {
  local db="$1"; shift
  compose exec -T postgres sh -c 'exec psql -X -q -At -F"|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"' sh "$db" "$@"
}

# Подключение к служебной БД postgres (для CREATE/DROP DATABASE).
psql_admin() { psql_db postgres "$@"; }

db_exists() {
  local out
  out="$(printf "SELECT 1 FROM pg_database WHERE datname = '%s';" "$1" | psql_admin)" || die "не удалось обратиться к postgres"
  [ "$out" = "1" ]
}

create_db() { printf 'CREATE DATABASE %s;' "$1" | psql_admin >/dev/null; }
drop_db()   { printf 'DROP DATABASE IF EXISTS %s WITH (FORCE);' "$1" | psql_admin >/dev/null; }

# Формат дампа по первым байтам: custom (pg_dump -Fc) | gzip | plain (SQL-текст).
dump_format() {
  local magic
  magic="$(od -An -tx1 -N5 "$1" | tr -d ' \n')"
  case "$magic" in
    5047444d50) echo custom ;;
    1f8b*)      echo gzip ;;
    *)          echo plain ;;
  esac
}

# Восстановить дамп в УЖЕ созданную пустую БД. Использование: restore_dump <файл> <имя БД>
restore_dump() {
  local file="$1" db="$2" fmt
  fmt="$(dump_format "$file")"
  log "формат дампа: $fmt"
  case "$fmt" in
    custom)
      compose exec -T postgres sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-privileges --exit-on-error' sh "$db" < "$file" ;;
    gzip)
      gzip -dc "$file" | compose exec -T postgres sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -f -' sh "$db" >/dev/null ;;
    plain)
      compose exec -T postgres sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -f -' sh "$db" < "$file" >/dev/null ;;
  esac
}

# Число строк по каждой таблице схемы public (кроме alembic_version): «таблица|число», по алфавиту.
count_rows() {
  psql_db "$1" <<'SQL'
SELECT t.table_name,
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', t.table_schema, t.table_name), false, true, '')))[1]::text
FROM information_schema.tables t
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND t.table_name <> 'alembic_version'
ORDER BY t.table_name;
SQL
}

current_revision() { printf 'SELECT version_num FROM alembic_version;' | psql_db "$1"; }
