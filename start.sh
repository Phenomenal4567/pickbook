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
  baseline_status=0
  python - <<'PY' || baseline_status=$?
import sys

from sqlalchemy import create_engine, inspect, text

from app.core.config import settings


connect_args = {}
database_url = settings.sqlalchemy_database_url
if database_url.startswith("postgres"):
    connect_args = {
        "sslmode": settings.database_sslmode,
        "connect_timeout": settings.database_connect_timeout_seconds,
        "prepare_threshold": None,
    }
elif database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)

with engine.connect() as connection:
    inspector = inspect(connection)
    has_stories = inspector.has_table("stories")
    has_alembic_version = inspector.has_table("alembic_version")

    if not has_stories:
        sys.exit(0)

    if not has_alembic_version:
        sys.exit(10)

    version_count = connection.execute(
        text("SELECT COUNT(*) FROM alembic_version")
    ).scalar_one()
    if version_count == 0:
        sys.exit(10)
PY

  if [[ "$baseline_status" -eq 10 ]]; then
    echo "[database] legacy schema detected without Alembic version; stamping current head"
    alembic stamp head
  elif [[ "$baseline_status" -ne 0 ]]; then
    exit "$baseline_status"
  fi

  alembic upgrade head
fi

uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
