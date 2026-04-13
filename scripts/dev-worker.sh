#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../services/api"
echo "Starting arq worker..."
uv run python -m arq api.gmail.workers.WorkerSettings
