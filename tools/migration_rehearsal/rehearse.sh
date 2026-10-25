#!/usr/bin/env bash
# Репетиция миграций на копии рабочей базы. НИЧЕГО не меняет в рабочей БД.
#
#   bash tools/migration_rehearsal/rehearse.sh --dump ФАЙЛ_ДАМПА --db rehearsal_ИМЯ [--keep]
#
# Что делает: создаёт временную БД rehearsal_* -> восстанавливает в неё дамп -> считает строки -> alembic upgrade head ->
# сверяет строки до/после -> проверяет целостность (master_id, «осиротевшие» связи) -> alembic check ->
# downgrade на 1 шаг и снова upgrade -> удаляет временную БД.
#
# Дамп: pg_dump в любом формате (custom .dump, обычный .sql, .sql.gz) — формат определяется по содержимому.
# Compose-файл: по умолчанию docker-compose.prod.yml (COMPOSE_FILE=... — другой). Контейнер postgres должен быть запущен.
# Защита: имя временной БД обязано начинаться с «rehearsal_»; рабочая БД не затрагивается; пароли не печатаются
# (строка подключения собирается внутри контейнера из его переменных окружения).
set -Eeuo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

DUMP="" DB="" KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dump) [ $# -ge 2 ] || die "после --dump нужен путь к файлу"; DUMP="$2"; shift 2 ;;
    --db)   [ $# -ge 2 ] || die "после --db нужно имя временной базы"; DB="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "неизвестный аргумент: $1 (см. --help)" ;;
  esac
done
[ -n "$DUMP" ] && [ -n "$DB" ] || die "нужны оба аргумента: --dump ФАЙЛ --db rehearsal_ИМЯ (см. --help)"

DUMP="$(abs_path "$DUMP")"
[ -f "$DUMP" ] && [ -s "$DUMP" ] || die "файл дампа не найден или пуст: $DUMP"

PROBLEMS=0
problem() { PROBLEMS=$((PROBLEMS + 1)); printf '  ✗ %s\n' "$*"; }
okline()  { printf '  ✓ %s\n' "$*"; }

TMP_DIR="$(mktemp -d)"
DB_CREATED=0
cleanup() {
  local code=$?
  rm -rf "$TMP_DIR"
  if [ "$DB_CREATED" = 1 ]; then
    if [ "$KEEP" = 1 ]; then
      log "временная база $DB ОСТАВЛЕНА (--keep). Удалить: printf 'DROP DATABASE %s WITH (FORCE);' $DB | docker compose -f $COMPOSE_FILE exec -T postgres sh -c 'psql -U \"\$POSTGRES_USER\" -d postgres'"
    else
      drop_db "$DB" && log "временная база $DB удалена" || log "НЕ УДАЛОСЬ удалить временную базу $DB — удалите вручную"
    fi
  fi
  exit "$code"
}
trap cleanup EXIT

alembic_in_db() {
  local db="$1"; shift
  compose run --rm --no-deps -T app sh -c \
    'export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/$1"; shift; exec alembic "$@"' \
    sh "$db" "$@"
}

# Сравнение числа строк: печатает таблицу и считает расхождения. Использование: compare_counts <до> <после> <заголовок>
compare_counts() {
  local before="$1" after="$2" title="$3" report diffs
  report="$(awk -F'|' '
    NR == FNR { b[$1] = $2; next }
    { a[$1] = $2 }
    END {
      for (t in b) {
        if (!(t in a))      printf "%-30s %10s %10s  ТАБЛИЦА УДАЛЕНА МИГРАЦИЕЙ\n", t, b[t], "-"
        else if (a[t] != b[t]) printf "%-30s %10s %10s  РАЗЛИЧИЕ\n", t, b[t], a[t]
        else                printf "%-30s %10s %10s\n", t, b[t], a[t]
      }
      for (t in a) if (!(t in b)) printf "%-30s %10s %10s  новая таблица\n", t, "-", a[t]
    }' "$before" "$after" | sort)"
  echo "  $title"
  echo "  таблица                                до      после"
  printf '%s\n' "$report" | sed 's/^/  /'
  diffs="$(printf '%s\n' "$report" | grep -c 'РАЗЛИЧИЕ$' || true)"
  if [ "$diffs" -gt 0 ]; then
    problem "число строк изменилось в таблицах: $diffs (если это задумано миграцией — проверьте её вручную)"
  else
    okline "число строк во всех таблицах совпадает"
  fi
}

# Целостность: master_id не NULL (значения и сама колонка), нет «осиротевших» строк по внешним ключам.
check_integrity() {
  local out kind subject n fk_total=0 mid_total=0 bad=0
  out="$(psql_db "$DB" <<'SQL'
CREATE TEMP TABLE _chk (kind text, subject text, n bigint);
INSERT INTO _chk
SELECT 'master_id_null_rows', c.table_name::text,
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I WHERE %I IS NULL', c.table_schema, c.table_name, c.column_name), false, true, '')))[1]::text::bigint
FROM information_schema.columns c
JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
WHERE c.table_schema = 'public' AND c.column_name = 'master_id';
INSERT INTO _chk
SELECT 'master_id_column_nullable', c.table_name::text, 1
FROM information_schema.columns c
JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
WHERE c.table_schema = 'public' AND c.column_name = 'master_id' AND c.is_nullable = 'YES';
DO $do$
DECLARE r record; cond text; nn text; q text; n bigint;
BEGIN
  FOR r IN
    SELECT con.conname, con.conrelid, con.confrelid, con.conkey, con.confkey
    FROM pg_constraint con JOIN pg_namespace ns ON ns.oid = con.connamespace
    WHERE con.contype = 'f' AND ns.nspname = 'public'
  LOOP
    SELECT string_agg(format('c.%I = p.%I', ca.attname, pa.attname), ' AND ' ORDER BY k.ord),
           string_agg(format('c.%I IS NOT NULL', ca.attname), ' AND ' ORDER BY k.ord)
      INTO cond, nn
    FROM unnest(r.conkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN unnest(r.confkey) WITH ORDINALITY AS f(attnum, ord) ON f.ord = k.ord
    JOIN pg_attribute ca ON ca.attrelid = r.conrelid AND ca.attnum = k.attnum
    JOIN pg_attribute pa ON pa.attrelid = r.confrelid AND pa.attnum = f.attnum;
    q := format('SELECT count(*) FROM %s c WHERE %s AND NOT EXISTS (SELECT 1 FROM %s p WHERE %s)',
                r.conrelid::regclass, nn, r.confrelid::regclass, cond);
    EXECUTE q INTO n;
    INSERT INTO _chk VALUES ('orphan_fk', r.conrelid::regclass::text || ' / ' || r.conname, n);
  END LOOP;
END
$do$;
SELECT kind, subject, n FROM _chk ORDER BY kind, subject;
SQL
)" || { problem "проверка целостности не выполнилась (ошибка SQL)"; return; }

  while IFS='|' read -r kind subject n; do
    [ -n "$kind" ] || continue
    case "$kind" in
      master_id_null_rows)
        mid_total=$((mid_total + 1))
        if [ "$n" -gt 0 ]; then problem "master_id = NULL: таблица $subject, строк: $n"; bad=1; fi ;;
      master_id_column_nullable)
        problem "колонка master_id допускает NULL: таблица $subject"; bad=1 ;;
      orphan_fk)
        fk_total=$((fk_total + 1))
        if [ "$n" -gt 0 ]; then problem "«осиротевшие» строки: $subject, строк: $n"; bad=1; fi ;;
    esac
  done <<< "$out"
  [ "$bad" = 0 ] && okline "целостность: master_id не NULL в $mid_total таблицах, «осиротевших» строк нет (проверено внешних ключей: $fk_total)"
  return 0
}

# ---------------------------------------------------------------------------------------------------------------------
log "Репетиция миграций: compose-файл $COMPOSE_FILE, временная база $DB"
echo "[1/8] Проверки"
require_docker
validate_temp_db_name "$DB" "rehearsal_"
require_postgres_running
if db_exists "$DB"; then die "база $DB уже существует — выберите другое имя (скрипт чужие базы не удаляет)"; fi
okline "docker и postgres доступны, имя базы допустимо, файл дампа: $(basename "$DUMP")"

echo "[2/8] Сборка образа приложения (код этапа — тот, что пойдёт в работу)"
compose build --quiet app >/dev/null
okline "образ собран"

echo "[3/8] Создание временной базы и восстановление дампа"
create_db "$DB"; DB_CREATED=1
restore_dump "$DUMP" "$DB" || die "не удалось восстановить дамп в $DB (файл повреждён или создан другим способом)"
okline "дамп восстановлен"

echo "[4/8] Состояние до миграций"
printf 'SELECT count(*) FROM alembic_version;' | psql_db "$DB" >"$TMP_DIR/ver_rows" 2>/dev/null || die "в дампе нет таблицы alembic_version — это дамп не этого проекта"
[ "$(cat "$TMP_DIR/ver_rows")" = "1" ] || die "в alembic_version должна быть ровно одна строка"
REV_BEFORE="$(current_revision "$DB")"
count_rows "$DB" >"$TMP_DIR/counts_before"
HEAD_REV="$(alembic_in_db "$DB" heads 2>/dev/null | awk 'NR==1 {print $1}')"
[ -n "$HEAD_REV" ] || die "alembic heads не вернул версию (несколько голов миграций?)"
okline "версия в дампе: $REV_BEFORE; версия в коде (head): $HEAD_REV; таблиц с данными: $(wc -l <"$TMP_DIR/counts_before" | tr -d ' ')"

echo "[5/8] alembic upgrade head"
if alembic_in_db "$DB" upgrade head >"$TMP_DIR/up1.out" 2>&1; then
  sed 's/^/    /' "$TMP_DIR/up1.out"
else
  sed 's/^/    /' "$TMP_DIR/up1.out"
  die "alembic upgrade head завершился с ошибкой — на рабочей базе будет то же самое"
fi
[ "$(current_revision "$DB")" = "$HEAD_REV" ] && okline "база на версии head ($HEAD_REV)" || problem "после upgrade версия базы не равна head"

echo "[6/8] Строки до/после и целостность"
count_rows "$DB" >"$TMP_DIR/counts_after"
compare_counts "$TMP_DIR/counts_before" "$TMP_DIR/counts_after" "Строки по таблицам (до миграции и после):"
check_integrity

echo "[7/8] alembic check (модели и схема после миграций не расходятся)"
if alembic_in_db "$DB" check >"$TMP_DIR/check.out" 2>&1; then
  okline "alembic check: расхождений нет"
else
  problem "alembic check нашёл расхождения между моделями и схемой:"; sed 's/^/      /' "$TMP_DIR/check.out"
fi

echo "[8/8] downgrade на 1 шаг и повторный upgrade"
alembic_in_db "$DB" downgrade -1 >"$TMP_DIR/down.out" 2>&1 && okline "downgrade -1 выполнен" || { problem "alembic downgrade -1 завершился с ошибкой:"; sed 's/^/      /' "$TMP_DIR/down.out"; }
alembic_in_db "$DB" upgrade head >"$TMP_DIR/up2.out" 2>&1 && okline "повторный upgrade head выполнен" || { problem "повторный upgrade head завершился с ошибкой:"; sed 's/^/      /' "$TMP_DIR/up2.out"; }
count_rows "$DB" >"$TMP_DIR/counts_cycle"
compare_counts "$TMP_DIR/counts_after" "$TMP_DIR/counts_cycle" "Строки после цикла downgrade/upgrade:"
check_integrity

echo
if [ "$PROBLEMS" -eq 0 ]; then
  log "РЕПЕТИЦИЯ ПРОЙДЕНА: миграции $REV_BEFORE -> $HEAD_REV безопасны на этой копии рабочей базы."
  exit 0
fi
log "РЕПЕТИЦИЯ НЕ ПРОЙДЕНА: проблем — $PROBLEMS. Запускать новую версию на рабочей базе НЕЛЬЗЯ, пока они не разобраны."
exit 1
