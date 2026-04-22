#!/usr/bin/env bash
# Deploy the API service and/or the arq worker to Railway, then (for api
# deploys) refresh the GCP Workspace add-on deployment descriptor so Gmail
# points at the new Railway URL.
#
# Services per environment:
#   staging:     api=api,       worker=staging-arq-worker
#   production:  api=prod-api,  worker=arq-worker
#
# The api service is deployed first because it owns database migrations
# (see services/api/Dockerfile CMD). The worker starts after migrations land.

set -euo pipefail

ENV="${1:-staging}"
FILTER="${2:-all}"

usage() {
  echo "Usage: ./scripts/deploy.sh [ENV] [FILTER]"
  echo "  ENV:    staging | production   (default: staging)"
  echo "  FILTER: all | api | worker     (default: all)"
  echo ""
  echo "Examples:"
  echo "  ./scripts/deploy.sh                      # all of staging"
  echo "  ./scripts/deploy.sh production           # all of production"
  echo "  ./scripts/deploy.sh staging worker       # just the staging worker"
  echo "  ./scripts/deploy.sh production api       # just the prod api"
  exit 1
}

if [[ "$ENV" != "staging" && "$ENV" != "production" ]]; then
  usage
fi
if [[ "$FILTER" != "all" && "$FILTER" != "api" && "$FILTER" != "worker" ]]; then
  usage
fi

if [[ "$ENV" == "production" ]]; then
  API_SERVICE="prod-api"
  WORKER_SERVICE="arq-worker"
  DEPLOYMENT_FILE="services/api/deployment.prod.json"
  DEPLOYMENT_NAME="lrp-scheduling-prod"
else
  API_SERVICE="api"
  WORKER_SERVICE="staging-arq-worker"
  DEPLOYMENT_FILE="services/api/deployment.staging.json"
  DEPLOYMENT_NAME="lrp-scheduling-staging"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Query the latest deployment status for a named service via `railway status --json`.
# Returns one of: SUCCESS | BUILDING | DEPLOYING | FAILED | CRASHED | UNKNOWN | NOT_FOUND.
get_deploy_status() {
  local svc="$1"
  railway status --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for env in data['environments']['edges']:
    if env['node']['name'] != '$ENV':
        continue
    for inst in env['node']['serviceInstances']['edges']:
        if inst['node']['serviceName'] != '$svc':
            continue
        latest = inst['node'].get('latestDeployment') or {}
        print(latest.get('status', 'UNKNOWN'))
        sys.exit(0)
print('NOT_FOUND')
"
}

# Poll until a service's latest deployment reaches a terminal state.
wait_for_deploy() {
  local svc="$1"
  local max_attempts=36  # 36 * 10s = 6 min
  local sleep_seconds=10

  echo "==> Waiting for $svc to become healthy..."
  for (( i=1; i<=max_attempts; i++ )); do
    local status
    status=$(get_deploy_status "$svc")

    case "$status" in
      SUCCESS)
        echo "==> $svc: SUCCESS"
        return 0
        ;;
      FAILED|CRASHED|REMOVED)
        echo "ERROR: $svc deploy reached terminal failure state: $status"
        return 1
        ;;
      NOT_FOUND)
        echo "ERROR: service '$svc' not found in env '$ENV' — create it in the Railway dashboard first."
        return 1
        ;;
      *)
        echo "   ... $svc status=$status (attempt $i/$max_attempts)"
        sleep "$sleep_seconds"
        ;;
    esac
  done

  echo "WARNING: $svc health check timed out after $((max_attempts * sleep_seconds))s."
  echo "   Check logs: railway logs -s $svc -e $ENV"
  return 1
}

deploy_service() {
  local svc="$1"
  echo ""
  echo "==> Deploying $svc to Railway ($ENV)..."
  cd "$REPO_ROOT/services/api"
  railway up -d -s "$svc" -e "$ENV"
  wait_for_deploy "$svc"
}

# --- Deploy api first (it owns migrations) ---
if [[ "$FILTER" == "all" || "$FILTER" == "api" ]]; then
  deploy_service "$API_SERVICE"
fi

# --- Then the worker (consumes the same DB, so it must follow) ---
if [[ "$FILTER" == "all" || "$FILTER" == "worker" ]]; then
  deploy_service "$WORKER_SERVICE"
fi

# --- Refresh the GCP add-on descriptor only when api was touched ---
# The descriptor points Gmail at the api service's public URL. The worker
# has no user-facing URL, so a worker-only deploy skips this step.
if [[ "$FILTER" == "all" || "$FILTER" == "api" ]]; then
  echo ""
  echo "==> Updating GCP add-on deployment ($DEPLOYMENT_NAME)..."
  cd "$REPO_ROOT"
  if ! gcloud workspace-add-ons deployments replace "$DEPLOYMENT_NAME" \
    --deployment-file="$DEPLOYMENT_FILE"; then
    echo ""
    echo "ERROR: Railway deployed but GCP add-on update failed. Manual intervention required:"
    echo "  gcloud workspace-add-ons deployments replace $DEPLOYMENT_NAME --deployment-file=$DEPLOYMENT_FILE"
    exit 1
  fi
fi

echo ""
echo "==> Done. Deployed $ENV ($FILTER) successfully."
