# RFC: Production Deployment & Environment Strategy

| Field         | Value           |
| ------------- | --------------- |
| **Author(s)** | Kinematic Labs  |
| **Status**    | Accepted        |
| **Created**   | 2026-04-07      |
| **Updated**   | 2026-04-07      |
| **Reviewers** | LRP Engineering |
| **Decider**   | Nim Sadeh       |

## Context and Scope

The LRP Scheduling Agent has been developed and tested using ngrok tunnels and a local Postgres database. It's time to deploy to production so that LRP coordinators can use it in their real Gmail environment at `longridgepartners.com`.

This is not a simple "push to Railway" task. The system has three layers of external configuration that must be coordinated:

1. **Railway** — hosting the FastAPI backend and Postgres database
2. **Google Cloud Platform** — OAuth client, Workspace Add-on deployment descriptor, and consent screen configuration
3. **Google Workspace Admin** — add-on installation and scoping to authorized users

Additionally, we need a coherent story for developing and testing new versions without breaking production. This means running a staging environment in parallel with production, each with its own database, OAuth configuration, and add-on deployment.

This RFC covers all three concerns: what to host, what to configure manually, and how to separate environments.

## Goals

- **G1: Production on Railway.** The FastAPI backend and Postgres database run on Railway under the `longridgepartners.com` domain, accessible to Google's add-on framework.
- **G2: Domain migration.** Move from the `kinematiclabs.dev` test domain to `longridgepartners.com` for all production OAuth and add-on configuration.
- **G3: Environment separation.** A staging environment exists alongside production, allowing end-to-end testing (including real Gmail sidebar interactions) without affecting production users or data.
- **G4: Reproducible setup.** This RFC serves as the definitive runbook — anyone with the right GCP/Railway access can set up or rebuild an environment by following it.

## Non-Goals

- **Custom domain for staging.** Staging will use the Railway-provided `*.up.railway.app` domain. Only production gets `longridgepartners.com`.
- **CI/CD pipeline.** Automated deployment pipelines are a follow-up. Initial deploys are manual via `railway up` or Railway's GitHub integration.
- **Redis / background workers.** Redis is listed as a dependency but unused in code. We'll skip provisioning it until background workers are built.
- **Marketplace publishing.** The add-on remains an internal deployment (not published to Google Workspace Marketplace).

---

## Design

### 1. What Lives on Railway

Railway hosts two services per environment:

| Service      | Type                   | Notes                                                        |
| ------------ | ---------------------- | ------------------------------------------------------------ |
| **api**      | Web service (Docker)   | FastAPI backend, serves add-on endpoints and OAuth callbacks |
| **postgres** | Railway Postgres addon | Managed Postgres, automatic backups                          |

That's it. No Redis, no workers, no separate migration service. Database migrations run as part of the container startup (see [Dockerfile Changes](#dockerfile-changes) below).

#### Railway Project Structure

```
Railway Project: lrp-scheduling-agent
├── Environment: production
│   ├── Service: api          (Docker, custom domain: schedule.longridgepartners.com)
│   └── Service: postgres     (Railway Postgres addon)
└── Environment: staging
    ├── Service: api          (Docker, Railway domain: lrp-staging-api-*.up.railway.app)
    └── Service: postgres     (Railway Postgres addon)
```

Railway's native environment feature gives us isolated Postgres instances and separate env vars per environment, with the same service definitions.

#### Railway Configuration

Create `railway.toml` at `services/api/`:

```toml
[build]
dockerfilePath = "Dockerfile"

[deploy]
healthcheckPath = "/health"
healthcheckTimeout = 30
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

#### Dockerfile Changes

The current Dockerfile does not run database migrations. We need to add a migration step before the server starts. Update the `CMD` to run migrations first:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

EXPOSE 8000

# Run migrations then start the server
CMD ["sh", "-c", "uv run yoyo apply --database \"$DATABASE_URL\" ./migrations --batch && uv run uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

Key changes:

- Migrations run via `yoyo apply --batch` (non-interactive) before uvicorn starts
- `${PORT:-8000}` — Railway injects `PORT`; we respect it with a fallback

#### Environment Variables on Railway

**Both environments (production + staging):**

| Variable                             | Value                                                              | Source                                                                                                    |
| ------------------------------------ | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| `DATABASE_URL`                       | `${{postgres.DATABASE_URL}}`                                       | Railway reference variable (auto-injected)                                                                |
| `ENVIRONMENT`                        | `production` or `staging`                                          | Set manually per environment                                                                              |
| `GMAIL_TOKEN_ENCRYPTION_KEY`         | Unique Fernet key per env                                          | Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `GOOGLE_OAUTH_CLIENT_ID`             | GCP OAuth client ID                                                | From GCP Console (same GCP project, different OAuth clients per env)                                      |
| `GOOGLE_OAUTH_CLIENT_SECRET`         | GCP OAuth client secret                                            | From GCP Console                                                                                          |
| `GCP_PROJECT_NUMBER`                 | `412595067134`                                                     | Same GCP project for both environments                                                                    |
| `GOOGLE_ADDON_SERVICE_ACCOUNT_EMAIL` | `service-412595067134@gcp-sa-gsuiteaddons.iam.gserviceaccount.com` | Same for both (GCP-managed)                                                                               |
| `SENTRY_DSN`                         | Sentry project DSN                                                 | From Sentry dashboard                                                                                     |

**Must NOT be set in production or staging:**

- `SKIP_ADDON_AUTH` — must be absent or `false`. Currently `true` in local `.env`.

---

### 2. What Must Be Configured Manually

These steps cannot be automated via Railway config. They require access to GCP Console and Google Workspace Admin Console.

#### 2a. GCP: OAuth Consent Screen

**Already done (verify settings):**

1. Go to [GCP Console → APIs & Services → OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)
2. Confirm **User type** is "Internal" (only `longridgepartners.com` domain users)
3. Confirm scopes include `gmail.modify`

**No changes needed** — the consent screen is shared across OAuth clients.

#### 2b. GCP: OAuth 2.0 Clients

We need **two** OAuth 2.0 clients — one per environment. This is critical: if staging and production share an OAuth client, a token obtained in staging could theoretically be used to access production Gmail data, and redirect URIs would conflict.

**Production OAuth Client:**

1. Go to [GCP Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)
2. Create a new OAuth 2.0 Client ID (or update the existing one)
   - Application type: **Web application**
   - Name: `LRP Scheduling Agent — Production`
   - Authorized redirect URIs: `https://schedule.longridgepartners.com/addon/oauth/callback`
3. Copy the Client ID and Client Secret → set as Railway env vars for `production`

**Staging OAuth Client:**

1. Create another OAuth 2.0 Client ID
   - Application type: **Web application**
   - Name: `LRP Scheduling Agent — Staging`
   - Authorized redirect URIs: `https://<staging-railway-domain>.up.railway.app/addon/oauth/callback`
2. Copy the Client ID and Client Secret → set as Railway env vars for `staging`

> **Important:** The exact staging Railway domain is assigned when you first deploy. You'll need to deploy once, note the domain, then come back and add it as an authorized redirect URI.

#### 2c. GCP: Workspace Add-on Deployment Descriptors

Google's Workspace Add-on framework requires a **deployment descriptor** that tells it where to send requests. We need two deployments — one pointing at production, one at staging.

**Production deployment descriptor** (`deployment.prod.json`):

```json
{
  "oauthScopes": [
    "https://www.googleapis.com/auth/gmail.addons.execute",
    "https://www.googleapis.com/auth/gmail.addons.current.message.metadata"
  ],
  "addOns": {
    "common": {
      "name": "LRP Scheduling Agent",
      "logoUrl": "https://schedule.longridgepartners.com/static/logo.png",
      "homepageTrigger": {
        "runFunction": "https://schedule.longridgepartners.com/addon/homepage"
      }
    },
    "gmail": {
      "contextualTriggers": [
        {
          "unconditional": {},
          "onTriggerFunction": "https://schedule.longridgepartners.com/addon/on-message"
        }
      ]
    }
  }
}
```

**Staging deployment descriptor** (`deployment.staging.json`):

```json
{
  "oauthScopes": [
    "https://www.googleapis.com/auth/gmail.addons.execute",
    "https://www.googleapis.com/auth/gmail.addons.current.message.metadata"
  ],
  "addOns": {
    "common": {
      "name": "LRP Scheduling Agent [STAGING]",
      "logoUrl": "https://<staging-domain>.up.railway.app/static/logo.png",
      "homepageTrigger": {
        "runFunction": "https://<staging-domain>.up.railway.app/addon/homepage"
      }
    },
    "gmail": {
      "contextualTriggers": [
        {
          "unconditional": {},
          "onTriggerFunction": "https://<staging-domain>.up.railway.app/addon/on-message"
        }
      ]
    }
  }
}
```

**Register each deployment via gcloud:**

```bash
# Production
gcloud workspace-add-ons deployments create lrp-scheduling-prod \
  --deployment-file=deployment.prod.json

# Staging
gcloud workspace-add-ons deployments create lrp-scheduling-staging \
  --deployment-file=deployment.staging.json
```

To update an existing deployment:

```bash
gcloud workspace-add-ons deployments replace lrp-scheduling-prod \
  --deployment-file=deployment.prod.json
```

#### 2d. Google Workspace Admin: Add-on Installation

Add-ons must be installed via the Google Workspace Admin Console to appear in users' Gmail.

1. Go to [Google Workspace Admin Console → Apps → Google Workspace Marketplace apps → Add app → Add internal app](https://admin.google.com/ac/apps/gmail)
2. Enter the **Deployment ID** from `gcloud workspace-add-ons deployments list`
3. **Scope the installation:**
   - **Production:** Install for all coordinators (or a Google Group containing coordinators)
   - **Staging:** Install for a test Google Group containing only developers/testers

> **Critical:** Both production and staging add-ons will appear in users' Gmail sidebars if they're in both groups. Name the staging add-on `[STAGING]` so testers can distinguish them.

#### 2e. DNS: Custom Domain for Production

Add a CNAME record on `longridgepartners.com`:

| Type  | Name       | Value                                                          | TTL |
| ----- | ---------- | -------------------------------------------------------------- | --- |
| CNAME | `schedule` | Railway-provided CNAME target (e.g., `<hash>.dns.railway.app`) | 300 |

Then in Railway:

1. Go to the `api` service in the `production` environment
2. Settings → Networking → Custom Domain → Add `schedule.longridgepartners.com`
3. Railway will verify the CNAME and provision a TLS certificate automatically

---

### 3. Environment Separation Strategy

#### The Problem

A Gmail add-on is installed at the Workspace level — it's not like a web app where you can just visit a different URL. When a coordinator opens Gmail, Google's framework calls whichever URL is registered in the deployment descriptor. We can't have a coordinator accidentally trigger staging endpoints or vice versa.

#### The Solution: Two Deployments, Two User Groups

```
┌─────────────────────────────────────────────────────────────────┐
│                    GCP Project: 412595067134                     │
│                                                                  │
│  ┌──────────────────────┐    ┌──────────────────────────────┐   │
│  │ Deployment: prod     │    │ Deployment: staging           │   │
│  │ → schedule.lrp.com   │    │ → lrp-staging.up.railway.app │   │
│  └──────────┬───────────┘    └──────────────┬───────────────┘   │
│             │                                │                   │
│  ┌──────────▼───────────┐    ┌──────────────▼───────────────┐   │
│  │ Installed for:       │    │ Installed for:               │   │
│  │ "LRP Coordinators"   │    │ "LRP Dev Team"              │   │
│  │ Google Group         │    │ Google Group                  │   │
│  └──────────────────────┘    └──────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘

          │                                │
          ▼                                ▼

┌──────────────────────┐    ┌──────────────────────────────┐
│ Railway: production   │    │ Railway: staging              │
│ ┌──────────────────┐ │    │ ┌──────────────────────────┐ │
│ │ api service      │ │    │ │ api service              │ │
│ │ Postgres addon   │ │    │ │ Postgres addon           │ │
│ └──────────────────┘ │    │ └──────────────────────────┘ │
└──────────────────────┘    └──────────────────────────────┘
```

#### Dev/Test/Prod Workflow

| Activity               | Environment          | How                                                                                                                                                             |
| ---------------------- | -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Local development**  | Local                | `docker compose up`, `dev-api.sh`, ngrok. Uses `deployment.json` pointing at ngrok URL. `SKIP_ADDON_AUTH=true`.                                                 |
| **End-to-end testing** | Staging (Railway)    | Deploy to staging environment. Google calls staging URLs. Dev team Google Group sees the `[STAGING]` add-on in Gmail. Separate database, separate OAuth tokens. |
| **Production**         | Production (Railway) | Deploy to production environment. Coordinator Google Group sees the add-on. Real data, real emails.                                                             |

#### Deploying a New Version

```bash
# Test locally first
./scripts/dev-api.sh
# Run tests
uv run pytest

# Deploy to staging
cd services/api
railway environment staging
railway up

# Test in staging (open Gmail as a dev team member, interact with the [STAGING] add-on)

# Deploy to production
railway environment production
railway up
```

#### Data Isolation

- **Database:** Each Railway environment has its own Postgres addon → completely separate databases. Staging never touches production data.
- **Gmail tokens:** Stored in the database with Fernet encryption. Each environment has a different `GMAIL_TOKEN_ENCRYPTION_KEY`, so even if a token row were copied between environments, it couldn't be decrypted.
- **OAuth clients:** Separate GCP OAuth clients per environment. A staging refresh token cannot be used against the production client ID.

#### What Developers See in Gmail

Developers who are in both the "LRP Coordinators" and "LRP Dev Team" Google Groups will see **two** add-on icons in their Gmail sidebar:

- "LRP Scheduling Agent" → calls production
- "LRP Scheduling Agent [STAGING]" → calls staging

This is the intended behavior. It lets developers verify production is working while also testing new features on staging.

---

### 4. Token Audience Validation — A Subtle Gotcha

The `verify_google_addon_token` function in `addon/auth.py` uses the **request URL** as the expected audience:

```python
expected_audience = str(request.url)
```

Google sets the token audience to the URL it's calling. This means:

- When Google calls `https://schedule.longridgepartners.com/addon/homepage`, the audience is that exact URL
- When Google calls `https://staging.up.railway.app/addon/homepage`, the audience is that URL

This works correctly as-is — no code changes needed. But it's worth noting: **if Railway's ingress sends a different `Host` header than what Google used**, audience validation will fail. Railway preserves the original `Host` header by default, so this should not be an issue, but it's the first thing to check if you get 401s in production.

Similarly, the OAuth redirect URI is derived from the request URL:

```python
base = str(request.url).split("/addon/oauth/")[0]
redirect_uri = f"{base}/addon/oauth/callback"
```

This means the redirect URI will automatically match the domain the service is accessed from. No hardcoded URLs need to change between environments.

---

### 5. Logo URL Fix

The current `deployment.json` references `/logo.png`, but FastAPI serves static files at `/static/`. The deployment descriptors above already use the correct path (`/static/logo.png`). Verify the logo file exists at `services/api/static/logo.png`.

---

## Step-by-Step Deployment Runbook

### Phase 1: Railway Setup

1. **Install Railway CLI:** `npm install -g @railway/cli && railway login`
2. **Create project:** `railway init` (name: `lrp-scheduling-agent`)
3. **Create environments:** Railway creates `production` by default. Add `staging` via the Railway dashboard.
4. **Add Postgres:** In each environment, add the Postgres addon via the Railway dashboard.
5. **Deploy api service:**
   ```bash
   cd scheduling-agent/services/api
   railway link  # link to the project
   railway environment production
   railway up
   ```
6. **Note the Railway domain** assigned to the api service (e.g., `lrp-scheduling-prod-abc123.up.railway.app`)
7. **Repeat for staging:**
   ```bash
   railway environment staging
   railway up
   ```
8. **Note the staging domain.**

### Phase 2: DNS & Custom Domain

9. **Add CNAME record** for `schedule.longridgepartners.com` → Railway's CNAME target
10. **Configure custom domain** in Railway dashboard for the production api service
11. **Wait for TLS provisioning** (usually < 5 minutes)
12. **Verify:** `curl https://schedule.longridgepartners.com/health`

### Phase 3: GCP Configuration

13. **Create/update OAuth clients** (Section 2b above) — one for production, one for staging
14. **Set Railway environment variables** (Section 1, env var table) for both environments. Remember to generate unique `GMAIL_TOKEN_ENCRYPTION_KEY` per environment.
15. **Redeploy** both environments so they pick up the new env vars:
    ```bash
    railway environment production && railway up
    railway environment staging && railway up
    ```
16. **Create deployment descriptors** (`deployment.prod.json`, `deployment.staging.json`) with correct URLs
17. **Register deployments:**
    ```bash
    gcloud workspace-add-ons deployments create lrp-scheduling-prod \
      --deployment-file=deployment.prod.json
    gcloud workspace-add-ons deployments create lrp-scheduling-staging \
      --deployment-file=deployment.staging.json
    ```

### Phase 4: Google Workspace Admin

18. **Create Google Groups** (if they don't already exist):
    - `coordinators@longridgepartners.com` — production users
    - `dev-team@longridgepartners.com` — staging testers
19. **Install the production add-on** via Admin Console → scoped to coordinators group
20. **Install the staging add-on** via Admin Console → scoped to dev-team group

### Phase 5: Verification

21. **Open Gmail as a dev team member.** Verify the `[STAGING]` add-on icon appears in the sidebar.
22. **Click the add-on.** You should see the OAuth authorization prompt (since no tokens exist in the staging database yet).
23. **Complete OAuth flow.** After authorizing, the homepage card should render.
24. **Open a scheduling email.** The contextual trigger (`on-message`) should fire and display the message card.
25. **Repeat steps 21-24 for production** (as a coordinator).

### Phase 6: Cleanup

26. **Delete the old ngrok-based deployment:**
    ```bash
    gcloud workspace-add-ons deployments delete lrp-scheduling-agent
    ```
27. **Update `deployment.json`** in the repo to point at production (this file is the canonical reference, even though the actual registration is done via gcloud).
28. **Remove `ADDON_BASE_URL` from `.env`** — it's not used by the code.

---

## Resolved Questions

1. **No custom domain.** Production uses Railway-provided `*.up.railway.app` URL. Users only interact via the Gmail sidebar — they never navigate to the backend URL directly. This avoids DNS coordination with LRP.
2. **Same GCP project.** We continue using GCP project `412595067134`. LRP has no cloud infra team. When the engagement ends, we remove our user and LRP retains the project.
3. **No Sentry for now.** No users are logging in yet. We'll add Sentry before onboarding coordinators.
4. **Superadmin access.** Nim has superadmin access to LRP's Google Workspace — can install add-ons and manage groups directly.
5. **No token migration.** No users exist. Clean slate for production.
6. **No existing users or Google Groups.** Groups for coordinators and dev/test will be created fresh.

---

## Risks and Mitigations

| Risk                                                         | Impact                                                                     | Mitigation                                                                                                                                               |
| ------------------------------------------------------------ | -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OAuth redirect URI mismatch                                  | Users can't authorize; stuck on error page                                 | Test OAuth flow immediately after deployment. Add both `https://` and `http://` variants if needed (Railway always uses HTTPS, so this shouldn't apply). |
| Token audience mismatch (reverse proxy / CDN rewriting Host) | All add-on requests fail with 401                                          | Don't put a CDN or proxy in front of Railway. Verify `Host` header preservation on first deploy.                                                         |
| `SKIP_ADDON_AUTH=true` accidentally set in production        | Complete auth bypass — anyone can call add-on endpoints                    | Env var not set by default. Add a startup check that logs an ERROR if `SKIP_ADDON_AUTH=true` and `ENVIRONMENT=production`.                               |
| Migration fails on deploy, uvicorn never starts              | Service down                                                               | Railway health check will detect and roll back. Yoyo migrations are idempotent (won't re-apply already-applied migrations).                              |
| Staging and production add-ons confused by testers           | Tester actions affect production data                                      | Clear naming (`[STAGING]`), separate Google Groups, separate databases.                                                                                  |
| Fernet key lost                                              | All stored Gmail tokens unrecoverable; every coordinator must re-authorize | Store Fernet keys in Railway env vars (backed up by Railway). Document the key in a secure vault (1Password, etc.).                                      |

---

## Code Changes Required

The deployment requires minimal code changes:

1. **`Dockerfile`** — Add migration step to `CMD` (see Section 1)
2. **`deployment.json`** — Replace with `deployment.prod.json` content (or keep both files)
3. **`railway.toml`** — New file (see Section 1)
4. **Startup safety check** — Optional but recommended: log ERROR if `SKIP_ADDON_AUTH=true` and `ENVIRONMENT=production`
5. **Logo URL** — Verify `static/logo.png` exists and is served correctly

No application code changes are needed. The dynamic URL resolution in `addon/routes.py` means the OAuth flow and card actions will work on any domain automatically.

---

## Alternatives Considered

### Single environment with feature flags

Instead of separate staging/production Railway environments, we could run one environment and use feature flags to gate new behavior. **Rejected** because: (a) we'd share a database, risking production data corruption during testing, (b) Gmail add-on deployment descriptors can only point to one URL per deployment, so we'd still need separate deployments, and (c) feature flags add code complexity for a two-person team.

### Apps Script wrapper for the add-on

Use Apps Script as a thin proxy that forwards requests to our backend, hiding the backend URL from the deployment descriptor. **Rejected** because: this adds a latency hop, a second codebase to maintain, and the HTTP add-on model works fine without it.

### Separate GCP projects per environment

Create `lrp-prod` and `lrp-staging` GCP projects. **Deferred** — this is cleaner for long-term isolation but adds GCP project management overhead. The single project with separate deployment descriptors is sufficient for now. Can revisit when LRP's IT team takes ownership.

### Fly.io or other hosting

Railway is our standard stack. No reason to evaluate alternatives at this stage.
