# Sentry setup guide (ops)

This guide walks through the manual Sentry UI setup for the LRP scheduling
agent. It's written for the ops team and assumes you have admin access to the
Sentry organization. It does **not** cover code changes — those live in the API
and worker codebases and are handled separately.

Estimated time: 30–45 minutes first time.

## 0. Prerequisites

- Owner-level access to a Sentry organization at https://sentry.io (or our
  self-hosted instance, if applicable).
- Railway access for the scheduling-agent project (to set env vars).
- The environments we're going to set up: `production`, `staging`, `local`
  (local is optional — most devs skip Sentry locally).

If you do not have a Sentry org yet, create one at
https://sentry.io/signup/ — pick the **Developer (free)** plan. The free plan
is 1 seat, 5k errors/mo, 10k spans/mo, 30-day retention. That is enough for
one engineer running the app in production.

## 1. Create the project

1. In Sentry, click **Projects → Create Project**.
2. Platform: **Python → FastAPI**.
3. Alert frequency: **Alert me on every new issue** (we'll refine in step 5).
4. Project name: `lrp-scheduling-agent`.
5. Team: create a team called `lrp` if one doesn't exist, and assign the
   project to it.
6. Click **Create Project**. Sentry will show a DSN — copy it somewhere safe
   for step 3. It looks like `https://<key>@o<org>.ingest.sentry.io/<id>`.

> We are deliberately creating **one project**, not one per service. The API
> and the Arq worker share a database and deployment; separating them in
> Sentry makes correlating a request across them harder. We distinguish them
> via the `service` tag instead (set in code).

## 2. Configure environments

1. Go to **Settings → Projects → lrp-scheduling-agent → Environments**.
2. Make sure `production` and `staging` appear. If you don't see them yet,
   they'll populate automatically the first time each environment sends an
   event. You can also pre-create them here.
3. Hide any noisy environments you don't want in the default view (e.g.
   `local`) via the "Hidden environments" list.

## 3. Set the DSN in Railway

1. In Railway, open the scheduling-agent project.
2. For **each service** (`api` and any worker service), set these env vars:
   - `SENTRY_DSN` — the DSN from step 1.
   - `ENVIRONMENT` — `production` or `staging` depending on the service
     environment. (This is what the code reads to tag events.)
3. Redeploy the service. You should see the first events appear in Sentry
   within a minute.

## 4. Configure data scrubbing (important — privacy)

We handle candidate and coordinator emails, thread subjects, and sometimes
email body snippets. We must make sure Sentry does not retain this data.

1. Go to **Settings → Security & Privacy** at the **organization** level
   (scrubbing is configured org-wide).
2. Under **Data Scrubber**, toggle **ON**:
   - Require Data Scrubber: **on**
   - Use Default Scrubbers: **on**
   - Scrub IP Addresses: **on**
3. Under **Advanced Data Scrubbing**, add rules for PII we specifically want
   to mask. At minimum:
   - Method: **Mask** · Data Type: **Email Address** · Source: `**`
   - Method: **Mask** · Data Type: **Credit Card Number** · Source: `**`
     (defensive — we don't handle these, but belt and suspenders)
   - Method: **Remove** · Data Type: **Anything** · Source:
     `$frame.vars.candidate_email`
     (and any other var names we find that carry PII — update this list as
     the codebase grows)
4. Under **Allowed Domains**, leave empty (we accept events from anywhere —
   Railway hostnames change).

## 5. Set up alert rules

We want two classes of alerts:

### 5a. Issue alert: any new error in production

1. **Alerts → Create Alert → Issues**.
2. **When:** a new issue is created.
3. **If:** `event.environment` equals `production`.
4. **Then:** send a notification to an email (the ops on-call address) **and**
   to Slack if we have the Slack integration installed (Settings →
   Integrations → Slack).
5. Rate limit: 1 notification per issue per hour (prevents alert storms).
6. Name it: `prod: new issue`.

### 5b. Metric alert: error spike

1. **Alerts → Create Alert → Number of Errors**.
2. Filter: `environment:production`.
3. Condition: `errors > 20 in any 5 minute window`.
4. Action: same notification targets as 5a.
5. Name it: `prod: error spike`.

### 5c. (Optional) WhatsApp

Sentry has **no native WhatsApp integration**. If WhatsApp alerts are
required:

- Option A: Add a Sentry **Webhook** alert action pointing at a
  Twilio-backed endpoint we host. This is custom code, not a UI-only setup.
- Option B: Use Zapier's Sentry → WhatsApp zap. Lower engineering cost but
  adds a third-party dependency and latency.

Recommend we start with email + Slack and defer WhatsApp.

## 6. Performance monitoring

1. Go to **Performance** in the left nav — confirm events are coming in. The
   API is configured with `traces_sample_rate=0.2` (20% of transactions).
2. Star the most important transactions (e.g. `POST /addon/draft-email`,
   `worker.poll_gmail_history`) so they appear on the project dashboard.
3. Set a **performance alert** for transaction duration regressions:
   - **Alerts → Create Alert → Transaction Duration**.
   - Filter: `environment:production`.
   - Condition: `p95 > 5s over 10 minutes`.
   - Notification: same as 5a.

## 7. Quotas and spend controls

The free plan is 5k errors / 10k performance spans per month. If we exceed
these, Sentry drops events silently — we should be alerted *before* that
happens.

1. **Settings → Subscription → Usage & Billing**.
2. Under **On-Demand Budget**, leave at **$0** to guarantee no surprise
   charges. This means quota-exhausted events are dropped rather than billed.
3. Under **Usage Alerts**, set:
   - Alert at **80%** of monthly error quota → ops email.
   - Alert at **80%** of monthly performance quota → ops email.
4. If we start hitting 80% regularly, upgrade to the Team plan ($26/mo at
   time of writing) or tighten `traces_sample_rate` in the API code.

## 8. Team access

Free plan = 1 seat. If more than one engineer needs access:

- Option A: share a single login (not recommended — no audit trail).
- Option B: upgrade to the Team plan, then **Settings → Members → Invite
  Member** with role `Member` (not `Owner`) for additional engineers.

## 9. Integrations worth installing

Under **Settings → Integrations**:

- **GitHub** — links stack-traces to source code, lets issues reference
  commits. Install and link to the `lrp-scheduling-agent` repo.
- **Slack** — for alert delivery. Install at the workspace level, then
  connect the `#lrp-alerts` channel (create it first in Slack).
- **Railway** — there's no native Railway integration. Release tracking can
  be done by calling the Sentry CLI from Railway's deploy hook; ask eng
  before enabling.

## 10. Sanity check

Trigger a test event to confirm the pipeline works end-to-end:

1. In Railway, connect to the API service shell.
2. Run: `python -c "import sentry_sdk; sentry_sdk.capture_message('sentry test from ops', level='error')"`
3. In Sentry, check **Issues** — the test message should appear within ~30s
   tagged with `environment:production`.
4. Click through to confirm data scrubbing is applied (no raw emails in the
   breadcrumbs or stack locals).
5. Acknowledge the test issue and mark resolved.

## 11. Document the DSN

Once verified, record the DSN (not the secret — the DSN is considered
semi-public) in the team's password manager under `lrp-scheduling-agent /
sentry / DSN`. The code never logs the DSN, so rotating it requires only
updating the Railway env var and redeploying.

## What is NOT covered by this guide

- Adding structured logs / INFO log forwarding to Sentry Logs — handled in
  code when/if we decide to ship logs (see issue #25 discussion).
- Setting up a second project for multi-customer routing — we are explicitly
  single-project today.
- PagerDuty / Opsgenie — PRD non-goals.
- LLM tracing — handled by LangFuse, not Sentry.
