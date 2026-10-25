#!/bin/sh
set -e

echo "Running database migrations..."
i=0
until alembic upgrade head; do
  i=$((i+1))
  if [ "$i" -ge 15 ]; then
    echo "Migrations failed after $i attempts"
    exit 1
  fi
  echo "DB not ready, retry $i/15 in 3s..."
  sleep 3
done

echo "Starting bot..."
exec python -m app.main