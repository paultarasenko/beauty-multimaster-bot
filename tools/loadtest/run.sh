#!/usr/bin/env bash
# Прогон нагрузочного профиля с замерами docker stats и выпиской ошибок из лога приложения.
#   bash tools/loadtest/run.sh --profile normal --duration 60
# Все аргументы передаются в python -m tools.loadtest.run (см. README). Результаты — tools/loadtest/results/<дата>/.
set -euo pipefail
cd "$(dirname "$0")/../.."
export MSYS_NO_PATHCONV=1   # Git Bash на Windows не должен «переводить» пути в аргументах
COMPOSE=(docker compose -f docker-compose.loadtest.yml)

PROFILE=run
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[i]}" == "--profile" && $((i + 1)) -lt ${#args[@]} ]]; then PROFILE="${args[i + 1]}"; fi
done

DIR="tools/loadtest/results/$(date +%Y-%m-%d)"
mkdir -p "$DIR"
PREFIX="$DIR/$(date +%H%M%S)_${PROFILE}"
STATS="${PREFIX}_docker_stats.csv"
: >"$STATS"
START_ISO="$(date -u +%Y-%m-%dT%H:%M:%S)"
IDS="$("${COMPOSE[@]}" ps -q)"

# Фоновый замер CPU и памяти контейнеров стенда раз в секунду: «unix-время,контейнер,CPU%,память / лимит».
(
  while true; do
    NOW="$(date +%s)"
    # shellcheck disable=SC2086
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' $IDS 2>/dev/null | sed "s/^/${NOW},/" >>"$STATS" || true
    sleep 1
  done
) &
SAMPLER=$!
trap 'kill "$SAMPLER" 2>/dev/null || true' EXIT

set +e
"${COMPOSE[@]}" run --rm -T runner python -m tools.loadtest.run "$@" --out-prefix "$PREFIX" --docker-stats-file "$STATS"
CODE=$?
set -e
kill "$SAMPLER" 2>/dev/null || true

"${COMPOSE[@]}" logs --no-color --no-log-prefix --since "$START_ISO" app 2>&1 | grep -E "ERROR|WARNING|Traceback" | head -300 >"${PREFIX}_app_errors.txt" || true
echo "Строк ERROR/WARNING/Traceback в логе приложения за прогон: $(wc -l <"${PREFIX}_app_errors.txt") (файл ${PREFIX}_app_errors.txt)"
echo "Код завершения прогона: ${CODE} (0 — все критерии пройдены, 1 — есть непройденные)"
exit "$CODE"
