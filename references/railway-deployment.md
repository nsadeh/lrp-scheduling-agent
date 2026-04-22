# Railway Deployment Runbook

How the app is actually deployed on Railway, and the ordered checklist for getting the arq worker + Redis into both environments. Written 2026-04-22 off a live audit of the `lrp` Railway project.

For the env-var definitions themselves see [env-vars.md](env-vars.md).

---

## Current state (2026-04-22)

Captured via `railway status --json` against project `lrp`:

| Env          | Services                              | Worker? | Redis? |
| ------------ | ------------------------------------- | ------- | ------ |
| `production` | `prod-api`, `Postgres`                | No      | No     |
| `staging`    | `api`, `Postgres`, `Redis`            | No      | Yes    |

### Env vars currently set on the app services

Both `prod-api` (production) and `api` (staging) have the same 7 user-set vars:

```
DATABASE_URL
ENVIRONMENT
GCP_PROJECT_NUMBER                      ← unread by code; informational only
GMAIL_TOKEN_ENCRYPTION_KEY
GOOGLE_ADDON_SERVICE_ACCOUNT_EMAIL      ← unread by code; informational only
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
```

Plus Railway's built-in `RAILWAY_*` vars (auto-injected — do not set manually).

### What's missing from both api services

Required by current code on startup or first request:

```
LANGFUSE_PUBLIC_KEY         ← api/ai/langfuse_client.py:42 raises at startup if unset
LANGFUSE_SECRET_KEY         ← same
ANTHROPIC_API_KEY           ← required for any LLM call
OPENAI_API_KEY              ← secondary-model fallback
GOOGLE_AI_API_KEY           ← tertiary-model fallback
PUBSUB_TOPIC                ← Gmail watch renewal skips without it
PUBSUB_WEBHOOK_AUDIENCE     ← webhook OIDC verification fails without it
PUBSUB_SERVICE_ACCOUNT      ← optional; has a working default
REDIS_URL                   ← push pipeline disabled without it
```

Optional but recommended:
```
SENTRY_DSN
SENTRY_TRACES_SAMPLE_RATE
LANGFUSE_ENVIRONMENT        ← e.g. "production" / "staging" for trace tagging
LANGFUSE_PROMPT_LABEL       ← defaults to "production"; override per-env if you want staging to fetch draft prompts
INTERNAL_EMAIL_DOMAINS
```

> **Before changing anything**, pull `railway logs --service prod-api` and `railway logs --service api -e staging`. If the deployed revision is older than the LangFuse-required merge, the app is running fine on old code and our variable adds shouldn't break anything. If it's current code, there's a startup loop to investigate first — adding variables will fix it but you want to know that's what happened.

---

## Goal state

| Env          | Services                                              |
| ------------ | ----------------------------------------------------- |
| `production` | `prod-api`, `arq-worker`, `Postgres`, `Redis`         |
| `staging`    | `api`, `staging-arq-worker`, `Postgres`, `Redis`      |

Every app service (api + worker) in each env has the full required env-var set. Shared values (`DATABASE_URL`, `REDIS_URL`) use Railway reference variables so rotating a password in Postgres/Redis propagates automatically.

Both the api and the worker build from the same [services/api/Dockerfile](../services/api/Dockerfile). They are differentiated by Railway's config-as-code file:

- api services use [services/api/railway.toml](../services/api/railway.toml) (default) — runs the Dockerfile's `CMD` (migrations + uvicorn), healthcheck on `/health`.
- worker services use [services/api/railway.worker.toml](../services/api/railway.worker.toml) — overrides the start command to `uv run python -m arq api.gmail.workers.WorkerSettings`, no healthcheck (arq serves no HTTP, so the `/health` probe would restart-loop the worker).

> **Important — Railway config precedence**: values in the config-as-code file **override** the service's UI settings. Trying to clear the healthcheck in the UI for a worker pointed at `railway.toml` silently reverts on every deploy. The worker service must be pointed at `railway.worker.toml` via **Service Settings → Config-as-code File Path**.

---

## Deployment model

### Why api and worker are separate services

Don't try to run `uvicorn` and `arq` in one container. Railway's health check only watches PID 1, so if arq crashes while uvicorn stays up Railway reports green and the push pipeline silently dies. Separate services get separate health, logs, metrics, and restart policy.

### Shared Dockerfile, different start commands

Both services build from the same [services/api/Dockerfile](../services/api/Dockerfile). The api service uses the Dockerfile's default `CMD` (runs migrations then uvicorn). The worker service **overrides the start command** in Railway settings:

```
uv run python -m arq api.gmail.workers.WorkerSettings
```

Note: the worker must **not** run migrations. Migrations are owned by the api's startup — running them twice is idempotent (yoyo tracks applied migrations), but the race condition on first deploy is avoidable by letting api be the sole migrator.

### Reference variables

Railway's `${{ServiceName.VAR_NAME}}` syntax creates a live reference. Prefer these for anything that points at another Railway service:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
```

When you rotate the Postgres password or recreate Redis, every service referencing it picks up the new value on its next deploy. No manual copy-paste.

---

## Checklist — staging first

Do staging end-to-end, verify, then mirror to production. Assume you're running commands from the repo root with `railway` CLI authenticated.

### S1. Link to staging

```bash
railway link -p lrp -e staging
```

### S2. Backfill missing env vars on the staging `api` service

```bash
railway service api
```

Set each missing var (prefer the dashboard for anything secret, since CLI values end up in shell history):

```bash
railway variables --set "LANGFUSE_PUBLIC_KEY=pk-lf-..." \
                  --set "LANGFUSE_SECRET_KEY=sk-lf-..." \
                  --set "LANGFUSE_HOST=https://us.cloud.langfuse.com" \
                  --set "LANGFUSE_ENVIRONMENT=staging" \
                  --set "ANTHROPIC_API_KEY=sk-ant-..." \
                  --set "OPENAI_API_KEY=sk-proj-..." \
                  --set "GOOGLE_AI_API_KEY=AIza..." \
                  --set "PUBSUB_TOPIC=projects/ai-agents-dev-492713/topics/gmail-push" \
                  --set "PUBSUB_WEBHOOK_AUDIENCE=https://api-staging-545f.up.railway.app/webhook/gmail" \
                  --set "PUBSUB_SERVICE_ACCOUNT=pubsub-push@ai-agents-dev-492713.iam.gserviceaccount.com" \
                  --set 'REDIS_URL=${{Redis.REDIS_URL}}'
```

Values above come from the developer's current local `.env` — confirm they're the right staging values (especially `PUBSUB_WEBHOOK_AUDIENCE`, which must match the staging api's public hostname).

> **Check**: `railway variables --kv` should list all of the above. The service will auto-redeploy; `railway logs` should show `LangFuse client initialized`, `Redis pool connected for push pipeline`, `ClassifierHook active`.

### S3. Create the staging `staging-arq-worker` service

Via dashboard: **New → Empty Service**, then in **Service Settings**:

- **Name**: `staging-arq-worker`
- **Root directory**: `services/api` (same as the api service; source is uploaded per deploy by `railway up`, no GitHub connection required)
- **Config-as-code File Path**: `railway.worker.toml`

That config file (checked into [services/api/railway.worker.toml](../services/api/railway.worker.toml)) sets the Dockerfile path, the arq start command, and the restart policy — and critically does **not** set `healthcheckPath`, so no HTTP probe runs.

> **Do not** set the start command or clear the healthcheck in the UI. Those fields will be overwritten by the config file on every deploy; if they mismatch, the config file wins and the UI silently lies about current state. Put all worker config in `railway.worker.toml`.

CLI equivalent for the service creation itself (exact flags vary by CLI version — dashboard is more reliable for first-time service creation):

```bash
railway add  # → select "empty service", name it "staging-arq-worker"
```

### S4. Copy all app env vars to the worker

The worker needs the same vars as api, **minus HTTP-only ones and plus `REDIS_URL`**. One-shot copy via the dashboard: go to the api service → Variables → "Copy all as JSON", paste into the worker service. Then:

- Remove `PUBSUB_WEBHOOK_AUDIENCE` (worker doesn't serve the webhook)
- Keep everything else

Required worker vars:

```
DATABASE_URL              ${{Postgres.DATABASE_URL}}
REDIS_URL                 ${{Redis.REDIS_URL}}
ENVIRONMENT               staging
GMAIL_TOKEN_ENCRYPTION_KEY  (same value as api)
GOOGLE_OAUTH_CLIENT_ID    (same value as api)
GOOGLE_OAUTH_CLIENT_SECRET (same value as api)
PUBSUB_TOPIC              projects/ai-agents-dev-492713/topics/gmail-push
LANGFUSE_PUBLIC_KEY       (same value as api)
LANGFUSE_SECRET_KEY       (same value as api)
LANGFUSE_HOST             (same value as api)
LANGFUSE_ENVIRONMENT      staging
ANTHROPIC_API_KEY         (same value as api)
OPENAI_API_KEY            (same value as api)
GOOGLE_AI_API_KEY         (same value as api)
SENTRY_DSN                (optional, same as api)
INTERNAL_EMAIL_DOMAINS    (optional, same as api)
```

### S5. Deploy and verify staging end-to-end

First-time and subsequent deploys both go through [scripts/deploy.sh](../scripts/deploy.sh), which deploys api then worker in order and waits for each to reach `SUCCESS` before moving on:

```bash
./scripts/deploy.sh staging                # both api + worker
./scripts/deploy.sh staging worker         # worker only
./scripts/deploy.sh staging api            # api only
```

Then watch logs:

```bash
railway logs -s staging-arq-worker -e staging
```

Expect on startup:
```
worker startup complete — ClassifierHook active
```

Then trigger a Gmail push (send a test email to a coordinator inbox registered in staging). The webhook in the api service should enqueue a `process_gmail_push` job; the worker logs should show it running.

If cron jobs are silent for >60s, check `poll_gmail_history` logs — it runs every 60s and will reveal auth/db issues even if no pushes are arriving.

---

## Checklist — production

Repeat for production, but **Redis doesn't exist yet**, so there's an extra step up front.

### P1. Link to production

```bash
railway link -p lrp -e production
```

### P2. Provision Redis

Dashboard: **New → Database → Add Redis**. Railway adds a `Redis` service with a managed volume and generates a `REDIS_URL` reference variable.

Wait for the Redis service to report SUCCESS before continuing.

### P3. Backfill env vars on `prod-api`

Same pattern as S2 but with production values:
- Fresh `LANGFUSE_*` keys if you want prod and staging in separate projects (recommended)
- `LANGFUSE_ENVIRONMENT=production`
- `PUBSUB_TOPIC` for the prod GCP project
- `PUBSUB_WEBHOOK_AUDIENCE=https://prod-api-production-6a67.up.railway.app/webhook/gmail` (confirm against the actual prod domain — may be `schedule.longridgepartners.com` if custom domain is configured)
- `REDIS_URL=${{Redis.REDIS_URL}}`

Generate a **new** `GMAIL_TOKEN_ENCRYPTION_KEY` for prod if the current one was shared with staging. If they've been sharing the same key, any re-key rotates every stored coordinator token — plan the cutover.

### P4. Create `arq-worker` service

Same as S3 but name it `arq-worker`. Don't forget to set the **Config-as-code File Path** to `railway.worker.toml` — without it, the worker will use the default `railway.toml` and crash-loop on the `/health` probe.

(Yes, the prod worker is the unprefixed name and the staging worker is `staging-arq-worker` — confusing, but [scripts/deploy.sh](../scripts/deploy.sh) hard-codes both.)

### P5. Copy vars to `arq-worker`

Same as S4, minus `PUBSUB_WEBHOOK_AUDIENCE`. Remember `LANGFUSE_ENVIRONMENT=production` on this service too.

### P6. Deploy and verify production end-to-end

```bash
./scripts/deploy.sh production
```

Watch it for 24h. The `renew_gmail_watches` cron fires 4×/day (hour 0/6/12/18) — you want to see at least one run succeed before declaring victory.

---

## Rollback

If the worker misbehaves:

- **Pause only**: `railway service <name>` → dashboard → "Disable service". API keeps running; jobs queue up in Redis.
- **Full rollback**: delete the worker service. Enqueued jobs in Redis will sit there; they're idempotent (`processed_messages` dedup), but the push pipeline is effectively down until a worker comes back. The `poll_gmail_history` cron won't run either (it's in the worker), so emails land in coordinator inboxes but no suggestions appear in the sidebar.

---

## Ongoing operations

- **Regular deploys**: `./scripts/deploy.sh staging` or `./scripts/deploy.sh production` — deploys api then worker in order and refreshes the GCP add-on descriptor. Accepts `api` or `worker` as a second arg to deploy just one side.
- **Adding a new env var**: add it to both services in both environments. Easy to forget the worker when you're debugging the api.
- **Rotating a secret** (API key, encryption key): change it on api and worker simultaneously. Redeploy both with `./scripts/deploy.sh <env>`.
- **Scaling**: api is stateless; add replicas via Railway's replica setting. Worker should stay at 1 replica unless throughput becomes a bottleneck — multiple arq workers off one Redis queue is supported but not tested in this app's code paths.
- **Monitoring the worker**: no health endpoint means no HTTP probe. Rely on Sentry (worker tags all events `service=worker`) and the `poll_gmail_history` heartbeat every 60s in logs.
