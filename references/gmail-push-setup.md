# Gmail Push Pipeline — Infrastructure Setup

This guide covers the Google Cloud and Railway infrastructure needed to run the Gmail push pipeline.

## Prerequisites

- `gcloud` CLI authenticated with access to the GCP project (`ai-agents-dev-492713`)
- Railway project with the API service deployed
- At least one coordinator with a stored OAuth token (via `gmail_oauth.py`)

## 1. Google Cloud Pub/Sub

### 1a. Create the Pub/Sub Topic

If the topic doesn't already exist:

```bash
gcloud pubsub topics create gmail-push \
  --project=ai-agents-dev-492713
```

### 1b. Grant Gmail Permission to Publish

Gmail's internal service account must be able to publish to the topic. Do this via the **GCP Console**:

1. Go to **Pub/Sub > Topics** > `gmail-push`
2. Click the **Permissions** tab (or "Show Info Panel" > Permissions)
3. Click **Add Principal**
4. Principal: `gmail-api-push@system.gserviceaccount.com`
5. Role: **Pub/Sub Publisher** (`roles/pubsub.publisher`)
6. Save

Or via CLI (requires `pubsub.topics.setIamPolicy` permission):

```bash
gcloud pubsub topics add-iam-policy-binding gmail-push \
  --project=ai-agents-dev-492713 \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

### 1c. Create a Service Account for Push Auth

This SA signs the OIDC tokens that our webhook verifies:

```bash
gcloud iam service-accounts create pubsub-push \
  --display-name="Pub/Sub Push Auth" \
  --project=ai-agents-dev-492713
```

Grant Pub/Sub permission to mint tokens with this SA:

```bash
# Get your project number (visible in GCP Console dashboard)
PROJECT_NUMBER=$(gcloud projects describe ai-agents-dev-492713 --format="value(projectNumber)")

gcloud iam service-accounts add-iam-policy-binding \
  pubsub-push@ai-agents-dev-492713.iam.gserviceaccount.com \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### 1d. Create the Push Subscription

This tells Pub/Sub to forward messages to our webhook:

**For staging:**
```bash
gcloud pubsub subscriptions create gmail-push-staging \
  --project=ai-agents-dev-492713 \
  --topic=gmail-push \
  --push-endpoint=https://api-staging-545f.up.railway.app/webhook/gmail \
  --push-auth-service-account=pubsub-push@ai-agents-dev-492713.iam.gserviceaccount.com \
  --push-auth-token-audience=https://api-staging-545f.up.railway.app/webhook/gmail
```

**For production** (replace domain when ready):
```bash
gcloud pubsub subscriptions create gmail-push-prod \
  --project=ai-agents-dev-492713 \
  --topic=gmail-push \
  --push-endpoint=https://PROD_DOMAIN/webhook/gmail \
  --push-auth-service-account=pubsub-push@ai-agents-dev-492713.iam.gserviceaccount.com \
  --push-auth-token-audience=https://PROD_DOMAIN/webhook/gmail
```

**For local dev (via ngrok):**
```bash
gcloud pubsub subscriptions create gmail-push-dev \
  --project=ai-agents-dev-492713 \
  --topic=gmail-push \
  --push-endpoint=https://poorly-dominant-redfish.ngrok-free.app/webhook/gmail \
  --push-auth-service-account=pubsub-push@ai-agents-dev-492713.iam.gserviceaccount.com \
  --push-auth-token-audience=https://poorly-dominant-redfish.ngrok-free.app/webhook/gmail
```

> **Note:** Only one push subscription should be active at a time per environment, or all environments will receive every notification. Use separate subscriptions and toggle them as needed.

## 2. Environment Variables

### Local Development (`.env`)

```bash
# Push pipeline
PUBSUB_TOPIC=projects/ai-agents-dev-492713/topics/gmail-push
REDIS_URL=redis://localhost:6379

# Webhook security
PUBSUB_WEBHOOK_AUDIENCE=https://poorly-dominant-redfish.ngrok-free.app/webhook/gmail
PUBSUB_SERVICE_ACCOUNT=pubsub-push@ai-agents-dev-492713.iam.gserviceaccount.com

# OAuth scopes (default is gmail.modify, add more as needed)
REQUIRED_SCOPES=https://www.googleapis.com/auth/gmail.modify
```

### Railway Staging

Set these in the Railway service variables:

| Variable | Value |
|----------|-------|
| `PUBSUB_TOPIC` | `projects/ai-agents-dev-492713/topics/gmail-push` |
| `REDIS_URL` | *(from Railway Redis addon — see section 3)* |
| `PUBSUB_WEBHOOK_AUDIENCE` | `https://api-staging-545f.up.railway.app/webhook/gmail` |
| `PUBSUB_SERVICE_ACCOUNT` | `pubsub-push@ai-agents-dev-492713.iam.gserviceaccount.com` |
| `REQUIRED_SCOPES` | `https://www.googleapis.com/auth/gmail.modify` |

## 3. Railway Redis

The push pipeline requires Redis for the arq job queue. Railway does not yet have a Redis instance.

### Add Redis to Railway

1. Go to your Railway project dashboard
2. Click **+ New** > **Database** > **Redis**
3. Railway provisions a managed Redis instance and exposes connection details
4. Copy the `REDIS_URL` from the Redis service's **Variables** tab (format: `redis://default:PASSWORD@HOST:PORT`)
5. Add `REDIS_URL` as a shared variable or reference it in the API service: `${{Redis.REDIS_URL}}`

### Deploy the arq Worker

The arq worker runs as a **separate Railway service** alongside the API:

1. In your Railway project, click **+ New** > **Service** (from same repo)
2. Set the **start command** to:
   ```
   cd services/api && PYTHONPATH=src python -m arq api.gmail.workers.WorkerSettings
   ```
3. Set the **root directory** to `/` (repo root)
4. Add the same environment variables as the API service (DATABASE_URL, REDIS_URL, GMAIL_TOKEN_ENCRYPTION_KEY, PUBSUB_TOPIC, etc.)
5. The worker does NOT need a public domain — it only reads from Redis

> **Important:** The worker must share the same `DATABASE_URL` and `REDIS_URL` as the API service. Use Railway's variable references (`${{Postgres.DATABASE_URL}}`, `${{Redis.REDIS_URL}}`) to keep them in sync.

## 4. Database Migration

Run the migration to add push pipeline columns:

```bash
# Local
cd services/api && uv run yoyo apply --batch \
  --database "${DATABASE_URL:-postgresql://dev:dev@localhost:5432/lrp_dev}" \
  ./migrations

# Railway (via railway run)
railway run --service=api -- \
  python -m yoyo apply --batch --database "$DATABASE_URL" ./migrations
```

## 5. Verification

After setup, verify the pipeline works:

1. **Watch registration**: The arq worker's `renew_gmail_watches` cron runs every 6 hours. To test immediately:
   ```bash
   cd services/api && uv run python scripts/test_push_pipeline.py \
     --user nim@longridgepartners.com
   ```

2. **Push notification flow**: Send an email to the coordinator's inbox. Within ~10 seconds, you should see the full email logged in the worker's stdout.

3. **Poll fallback**: Even without Pub/Sub configured, the worker polls every 60 seconds via `poll_gmail_history`. This is the safety net.

## Architecture Reference

```
Email arrives
    │
    ├─ Push path (~5s): Gmail → Pub/Sub → POST /webhook/gmail → arq job → hook
    │
    └─ Poll path (~60s): arq cron → history.list() per coordinator → hook
    
Both paths → _process_history() → dedup check → classify → EmailEvent → hook.on_email()
```
