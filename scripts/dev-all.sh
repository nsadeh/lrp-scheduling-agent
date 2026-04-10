#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_DIR="$REPO_ROOT/services/api"

# --- Start infra ---
cd "$REPO_ROOT"
docker compose up -d postgres redis

# --- Run the API server (includes ngrok swap + migrations) ---
./scripts/dev-api.sh &
API_PID=$!

# --- Start the arq worker ---
cd "$API_DIR"
export DATABASE_URL="${DATABASE_URL:-postgresql://dev:dev@localhost:5432/lrp_dev}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
export ENVIRONMENT="development"
export PYTHONPATH="src:${PYTHONPATH:-}"

# Small delay so the API server finishes migrations first
sleep 2
echo "==> Starting arq worker..."
uv run arq api.agent.worker_settings.WorkerSettings &
WORKER_PID=$!

# --- Clean up everything on exit ---
cleanup() {
  echo ""
  echo "==> Stopping worker and API..."
  kill "$WORKER_PID" 2>/dev/null || true
  kill "$API_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> API + worker running. Press Ctrl+C to stop."
wait
