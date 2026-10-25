#!/usr/bin/env bash
# Резервная копия рабочей базы: pg_dump (формат custom, со сжатием) -> проверка файла -> ротация старых копий.
#
#   bash tools/backup/backup.sh
#
# Переменные (необязательно): BACKUP_DIR (по умолчанию ./backups), RETENTION_DAYS (14), COMPOSE_FILE (docker-compose.prod.yml;
# для старого стека: COMPOSE_FILE=docker-compose.yml). Файл: backups/bbot_ГГГГММДД_ЧЧММСС.dump, права 600.
# BACKUP_OVERWRITE=1 — режим «одна копия»: файл backups/bbot_latest.dump, который каждый запуск заменяет новой, уже проверенной копией
# (замена атомарная: прежняя копия остаётся, пока новая не создана и не прочитана pg_restore). Без переменной — копии с датой и ротацией.
# В копию НЕ входят .env и BOT_TOKEN_ENCRYPTION_KEY — храните их отдельно (docs/OPERATIONS.md).
set -Eeuo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

BACKUP_DIR="$(abs_path "${BACKUP_DIR:-backups}")"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] && [ "$RETENTION_DAYS" -ge 1 ] || die "RETENTION_DAYS должно быть целым числом дней >= 1"

require_docker
require_postgres_running
umask 077
mkdir -p "$BACKUP_DIR"

# Защита от двух одновременных запусков (cron + ручной запуск).
LOCK_DIR="$BACKUP_DIR/.backup.lock"
mkdir "$LOCK_DIR" 2>/dev/null || die "резервное копирование уже идёт (или осталась блокировка $LOCK_DIR после сбоя — удалите её, если уверены, что других запусков нет)"

if [ "${BACKUP_OVERWRITE:-0}" = "1" ]; then NAME="latest"; else NAME="$(date '+%Y%m%d_%H%M%S')"; fi
FINAL="$BACKUP_DIR/bbot_${NAME}.dump"
PARTIAL="$FINAL.partial"
cleanup() { local code=$?; rmdir "$LOCK_DIR" 2>/dev/null || true; rm -f "$PARTIAL"; exit "$code"; }
trap cleanup EXIT

log "Копирование базы -> $(basename "$FINAL")"
compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6 --no-owner --no-privileges' >"$PARTIAL" \
  || die "pg_dump завершился с ошибкой — копия не создана"
[ -s "$PARTIAL" ] || die "pg_dump создал пустой файл"

# Проверка: pg_restore умеет прочитать оглавление файла (ловит обрезанные и повреждённые копии).
compose exec -T postgres sh -c 'exec pg_restore --list >/dev/null' <"$PARTIAL" || die "файл копии не читается pg_restore — копия НЕ принята"

mv "$PARTIAL" "$FINAL"
chmod 600 "$FINAL" 2>/dev/null || true
SIZE="$(wc -c <"$FINAL" | tr -d ' ')"
log "Готово: $FINAL ($SIZE байт), оглавление прочитано"

# Ротация: удаляем копии старше RETENTION_DAYS дней. Новая копия уже принята, так что хотя бы одна всегда остаётся.
OLD="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'bbot_*.dump' -mtime +"$RETENTION_DAYS" | wc -l | tr -d ' ')"
if [ "$OLD" -gt 0 ]; then
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'bbot_*.dump' -mtime +"$RETENTION_DAYS" -delete
  log "Ротация: удалено копий старше $RETENTION_DAYS дн.: $OLD"
fi
log "Копий в каталоге: $(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'bbot_*.dump' | wc -l | tr -d ' ')"
log "Напоминание: скопируйте свежую копию ещё и на другой диск/сервер — копия рядом с базой не спасает при потере сервера."
