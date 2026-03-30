#!/usr/bin/env bash
set -euo pipefail

# Ensure infra is running
docker compose up -d postgres redis

# Run migrations if any exist
cd services/api
if ls migrations/*.py &>/dev/null; then
  uv run yoyo apply --database "${DATABASE_URL:-postgresql://dev:dev@localhost:5432/lrp_dev}" ./migrations
fi

# Start API with hot reload
export DATABASE_URL="${DATABASE_URL:-postgresql://dev:dev@localhost:5432/lrp_dev}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
export ENVIRONMENT="development"
export PYTHONPATH="src:${PYTHONPATH:-}"
uv run uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
