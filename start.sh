#!/bin/bash
set -euo pipefail

PORT="${PORT:-8000}"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose up -d postgres redis

  echo "Waiting for Postgres and Redis health checks..."
  for _ in {1..60}; do
    postgres_status="$(docker compose ps --format json postgres 2>/dev/null | grep -o '"Health":"[^"]*"' | head -n1 || true)"
    redis_status="$(docker compose ps --format json redis 2>/dev/null | grep -o '"Health":"[^"]*"' | head -n1 || true)"

    if [[ "$postgres_status" == '"Health":"healthy"' && "$redis_status" == '"Health":"healthy"' ]]; then
      break
    fi

    sleep 2
  done
fi

if command -v alembic >/dev/null 2>&1; then
  alembic upgrade head
fi

uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
