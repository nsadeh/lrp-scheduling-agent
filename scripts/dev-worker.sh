#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../services/api"
export PYTHONPATH="src:${PYTHONPATH:-}"
# Load .env for local development (production injects env vars directly)
if [ -f .env ]; then
  set -a; source .env; set +a
fi
echo "Starting arq worker..."
uv run python -m arq api.gmail.workers.WorkerSettings
