#!/usr/bin/env bash
set -euo pipefail

echo "==> Starting infrastructure..."
docker compose up -d

echo "==> Setting up API service..."
cd services/api
uv sync
uv run pre-commit install
if [ -d "migrations" ] && ls migrations/*.py &>/dev/null; then
  echo "==> Running migrations..."
  uv run yoyo apply --batch --database "${DATABASE_URL:-postgresql://dev:dev@localhost:5432/lrp_dev}" ./migrations
fi
cd ../..

echo "==> Done! Run ./scripts/dev-api.sh to start developing."
