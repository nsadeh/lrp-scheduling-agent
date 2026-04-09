#!/usr/bin/env bash
set -euo pipefail

NGROK_DOMAIN="poorly-dominant-redfish.ngrok-free.app"
STAGING_DEPLOYMENT_NAME="lrp-scheduling-staging"
STAGING_GCP_PROJECT="ai-agents-dev-492713"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_DIR="$REPO_ROOT/services/api"
DEPLOYMENT_DEV="$API_DIR/deployment.dev.json"

# --- Generate a temporary dev deployment descriptor ---
sed "s|api-staging-545f.up.railway.app|${NGROK_DOMAIN}|g; s|LRP Scheduling Agent \[STAGING\]|LRP Scheduling Agent [DEV]|g" \
  "$API_DIR/deployment.staging.json" > "$DEPLOYMENT_DEV"

# --- Swap staging add-on to point at ngrok ---
echo "==> Pointing staging add-on at ${NGROK_DOMAIN}..."
gcloud workspace-add-ons deployments replace "$STAGING_DEPLOYMENT_NAME" \
  --project="$STAGING_GCP_PROJECT" \
  --deployment-file="$DEPLOYMENT_DEV" 2>&1

# --- Restore staging on exit (Ctrl+C, crash, normal exit) ---
restore_staging() {
  echo ""
  echo "==> Restoring staging add-on to Railway..."
  if gcloud workspace-add-ons deployments replace "$STAGING_DEPLOYMENT_NAME" \
    --project="$STAGING_GCP_PROJECT" \
    --deployment-file="$API_DIR/deployment.staging.json" 2>&1; then
    echo "==> Staging restored."
  else
    echo ""
    echo "ERROR: Failed to restore staging add-on! It may still point at the dead ngrok URL."
    echo "  Run manually:"
    echo "  gcloud workspace-add-ons deployments replace $STAGING_DEPLOYMENT_NAME \\"
    echo "    --project=$STAGING_GCP_PROJECT \\"
    echo "    --deployment-file=$API_DIR/deployment.staging.json"
  fi
  rm -f "$DEPLOYMENT_DEV"
}
trap restore_staging EXIT

# --- Ensure local infra is running ---
cd "$REPO_ROOT"
docker compose up -d postgres redis

# --- Run migrations ---
cd "$API_DIR"
if ls migrations/*.py &>/dev/null; then
  uv run yoyo apply --batch --database "${DATABASE_URL:-postgresql://dev:dev@localhost:5432/lrp_dev}" ./migrations
fi

# --- Start API with hot reload ---
export DATABASE_URL="${DATABASE_URL:-postgresql://dev:dev@localhost:5432/lrp_dev}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
export ENVIRONMENT="development"
export PYTHONPATH="src:${PYTHONPATH:-}"
echo "==> Starting dev server. Gmail [STAGING] add-on → https://${NGROK_DOMAIN}"
echo "==> Press Ctrl+C to stop and restore staging."
uv run uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
