#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../services/api"
export PYTHONPATH="src:${PYTHONPATH:-}"
echo "Starting arq worker..."
uv run python -m arq api.gmail.workers.WorkerSettings
