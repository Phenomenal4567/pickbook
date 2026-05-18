#!/bin/bash
set -e

echo "Starting PickBook infrastructure..."
docker-compose up -d

echo "Waiting for PostgreSQL to be healthy..."
until docker-compose exec -T postgres pg_isready -U "${POSTGRES_USER:-pickbook}" > /dev/null 2>&1; do
  echo "  postgres not ready — retrying in 2s..."
  sleep 2
done
echo "PostgreSQL ready."

echo "Waiting for Redis to be healthy..."
until docker-compose exec -T redis redis-cli ping > /dev/null 2>&1; do
  echo "  redis not ready — retrying in 2s..."
  sleep 2
done
echo "Redis ready."

PORT="${PORT:-8000}"
echo "Starting uvicorn on 0.0.0.0:$PORT ..."
# --reload removed: development only, not safe or performant in production
uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
