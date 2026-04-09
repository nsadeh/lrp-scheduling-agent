#!/usr/bin/env bash
set -euo pipefail

ENV="${1:-staging}"

if [[ "$ENV" != "staging" && "$ENV" != "production" ]]; then
  echo "Usage: ./scripts/deploy.sh [staging|production]"
  echo "  Defaults to staging if not specified."
  exit 1
fi

DEPLOYMENT_NAME="lrp-scheduling-${ENV/production/prod}"
DEPLOYMENT_FILE="services/api/deployment.${ENV/production/prod}.json"

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

echo ""
echo "==> Updating GCP add-on deployment ($DEPLOYMENT_NAME)..."
cd ../..
gcloud workspace-add-ons deployments replace "$DEPLOYMENT_NAME" \
  --deployment-file="$DEPLOYMENT_FILE"

echo ""
echo "==> Done. Deployed $ENV successfully."
