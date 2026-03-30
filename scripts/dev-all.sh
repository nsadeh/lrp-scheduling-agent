#!/usr/bin/env bash
set -euo pipefail

# Start infra
docker compose up -d

# Run API
./scripts/dev-api.sh &
API_PID=$!

# Trap to clean up on exit
trap "kill $API_PID 2>/dev/null; docker compose stop" EXIT

echo "All services running. Press Ctrl+C to stop."
wait
