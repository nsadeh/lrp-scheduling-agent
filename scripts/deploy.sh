#!/usr/bin/env bash
set -euo pipefail

ENV="${1:-staging}"

if [[ "$ENV" != "staging" && "$ENV" != "production" ]]; then
  echo "Usage: ./scripts/deploy.sh [staging|production]"
  echo "  Defaults to staging if not specified."
  exit 1
fi

if [[ "$ENV" == "production" ]]; then
  DEPLOYMENT_FILE="services/api/deployment.prod.json"
  DEPLOYMENT_NAME="lrp-scheduling-prod"
else
  DEPLOYMENT_FILE="services/api/deployment.staging.json"
  DEPLOYMENT_NAME="lrp-scheduling-staging"
fi

echo "==> Deploying to Railway ($ENV)..."
cd services/api
railway environment "$ENV"
railway up -d

# Poll for Railway service health before updating the GCP add-on descriptor.
echo "==> Waiting for Railway deployment to become healthy..."
MAX_ATTEMPTS=30
SLEEP_SECONDS=10
for (( i=1; i<=MAX_ATTEMPTS; i++ )); do
  if railway status 2>/dev/null | grep -qi "running\|success"; then
    echo "==> Railway deployment is healthy."
    break
  fi
  if [[ $i -eq $MAX_ATTEMPTS ]]; then
    echo "WARNING: Railway health check timed out after $((MAX_ATTEMPTS * SLEEP_SECONDS))s."
    echo "Proceeding with GCP update, but the service may not be ready."
  fi
  sleep "$SLEEP_SECONDS"
done

echo ""
echo "==> Updating GCP add-on deployment ($DEPLOYMENT_NAME)..."
cd ../..
if ! gcloud workspace-add-ons deployments replace "$DEPLOYMENT_NAME" \
  --deployment-file="$DEPLOYMENT_FILE"; then
  echo ""
  echo "ERROR: Railway deployed but GCP add-on update failed. Manual intervention required:"
  echo "  gcloud workspace-add-ons deployments replace $DEPLOYMENT_NAME --deployment-file=$DEPLOYMENT_FILE"
  exit 1
fi

echo ""
echo "==> Done. Deployed $ENV successfully."
