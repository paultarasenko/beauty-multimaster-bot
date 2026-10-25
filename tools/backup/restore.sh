#!/usr/bin/env bash
# Проверка копии: восстанавливает её во ВРЕМЕННУЮ базу, считает строки и удаляет базу. Рабочую БД не трогает.
#
#   bash tools/backup/restore.sh --dump backups/bbot_20261012_030000.dump --db restore_check_1 [--keep]
#   --dump latest  — взять самую свежую копию из BACKUP_DIR (по умолчанию ./backups)
#
# Имя временной базы обязано начинаться с «restore_check_». Формат дампа определяется по содержимому (.dump, .sql, .sql.gz).
# Восстановление в РАБОЧУЮ базу после аварии — вручную, по docs/OPERATIONS.md (этот скрипт его намеренно не делает).
set -Eeuo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

DUMP="" DB="" KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dump) [ $# -ge 2 ] || die "после --dump нужен путь к файлу"; DUMP="$2"; shift 2 ;;
    --db)   [ $# -ge 2 ] || die "после --db нужно имя временной базы"; DB="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "неизвестный аргумент: $1 (см. --help)" ;;
  esac
done
[ -n "$DUMP" ] && [ -n "$DB" ] || die "нужны оба аргумента: --dump ФАЙЛ --db restore_check_ИМЯ (см. --help)"
if [ "$DUMP" = "latest" ]; then
  DUMP="$(ls -t "$(abs_path "${BACKUP_DIR:-backups}")"/bbot_*.dump 2>/dev/null | head -1 || true)"
  [ -n "$DUMP" ] || die "в каталоге копий нет файлов bbot_*.dump"
fi
DUMP="$(abs_path "$DUMP")"
[ -f "$DUMP" ] && [ -s "$DUMP" ] || die "файл дампа не найден или пуст: $DUMP"

require_docker
validate_temp_db_name "$DB" "restore_check_"
require_postgres_running
if db_exists "$DB"; then die "база $DB уже существует — выберите другое имя (скрипт чужие базы не удаляет)"; fi

DB_CREATED=0
TMP_DIR="$(mktemp -d)"
cleanup() {
  local code=$?
  rm -rf "$TMP_DIR"
  if [ "$DB_CREATED" = 1 ]; then
    if [ "$KEEP" = 1 ]; then log "временная база $DB оставлена (--keep)"; else drop_db "$DB" && log "временная база $DB удалена" || log "НЕ УДАЛОСЬ удалить $DB — удалите вручную"; fi
  fi
  exit "$code"
}
trap cleanup EXIT

log "Восстановление $(basename "$DUMP") во временную базу $DB"
create_db "$DB"; DB_CREATED=1
restore_dump "$DUMP" "$DB" || die "восстановление не удалось: копия повреждена или несовместима"

REV="$(current_revision "$DB" 2>/dev/null)" || die "в копии нет таблицы alembic_version — это не копия базы проекта"
[ -n "$REV" ] || die "alembic_version пуста"
count_rows "$DB" >"$TMP_DIR/counts"
TABLES="$(wc -l <"$TMP_DIR/counts" | tr -d ' ')"
[ "$TABLES" -gt 0 ] || die "в копии нет таблиц с данными"

echo "  версия миграций в копии: $REV"
echo "  таблица                           строк"
awk -F'|' '{ printf "  %-30s %10s\n", $1, $2 }' "$TMP_DIR/counts"
log "КОПИЯ ВОССТАНАВЛИВАЕТСЯ: таблиц $TABLES, версия $REV. Сверьте числа с ожидаемыми (например, число записей)."
