# RFC: Gmail Async Push Pipeline

| Field         | Value                                 |
| ------------- | ------------------------------------- |
| **Author(s)** | Kinematic Labs                        |
| **Status**    | Draft                                 |
| **Created**   | 2026-04-13                            |
| **Updated**   | 2026-04-13                            |
| **Reviewers** | LRP Engineering, LRP Coordinator team |
| **Decider**   | Nim Sadeh                             |
| **Issue**     | #6                                    |

## Context and Scope

The scheduling agent needs to react to email traffic in near-real-time without coordinators opening messages. Today, the backend only processes emails when a coordinator opens a message with the Workspace Add-on sidebar visible — the contextual trigger in `addon/routes.py` calls the backend, which reads the thread and renders a card. This requires the coordinator to manually open every scheduling email, defeating the purpose of the agent.

PR #4 attempted to solve this by bundling a Gmail push pipeline with the full agent engine (LLM classification, draft generation, sidebar integration) in a single 6,700-line, 49-file change. The review found four high-severity bugs — an unauthenticated webhook, broken action buttons from an import bug, an unverified JWT, and a global state race condition. The PR is being abandoned.

This RFC extracts just the **Gmail push infrastructure** as a standalone, decoupled library layer. It adds no agent logic, no LLM calls, no scheduling state management. It provides a single async hook that downstream code (the agent engine, future features) can subscribe to. The agent reasoning, classification, and draft generation will be built on top of this foundation in a subsequent RFC.

### What Changes

- Coordinators' inboxes are watched via Gmail Pub/Sub push notifications
- A background worker processes new messages and fires an async hook
- A 60-second fallback poll guarantees no email is missed
- Incoming, outgoing, reply, forward, and new-thread messages are classified deterministically (no LLM)

### What Doesn't Change

- The existing Gmail client (`gmail/client.py`) API surface — we add methods, not modify existing ones
- The add-on sidebar flow — it continues to work as before
- OAuth token storage and encryption — unchanged
- The scheduling loop data model — untouched

## Goals

- **G1: React to emails within ~30 seconds.** When an email arrives in or is sent from a coordinator's mailbox, the pipeline processes it and fires the hook within 30 seconds under normal conditions.
- **G2: Never miss an email.** Even if the push notification is dropped, delayed, or the service restarts, every email is eventually processed. The 60-second fallback poll guarantees this.
- **G3: Expose a simple async hook interface.** Downstream code subscribes to email events by implementing a single protocol method. The hook receives a structured event with the parsed message, direction (incoming/outgoing), type (new/reply/forward), and any new participants.
- **G4: Classify direction and type without AI.** Incoming vs. outgoing, reply vs. forward vs. new thread — all determined by email headers and participant set comparison. No LLM, no heuristics.
- **G5: Decouple from agent logic.** This pipeline lives entirely in `api/gmail/`. It does not import from `api/agent/`, `api/scheduling/`, or any future AI modules. The default hook just logs.

## Non-Goals

- **Email classification by content.** "Is this a scheduling email?" is the agent's job, not the pipeline's. The pipeline fires for every email; filtering is the consumer's responsibility.
- **Draft generation or response suggestion.** The pipeline delivers events. What to do with them is the hook consumer's problem.
- **Email content storage.** We store message IDs for deduplication (30-day TTL) and history cursor state. We do not replicate email content into our database.
- **Scheduling state transitions.** The pipeline does not know about loops, stages, or scheduling workflows.
- **Google Workspace Events API.** As of April 2026, Gmail is not a supported event source for the Workspace Events API. Pub/Sub Watch remains the only first-party push mechanism.

## Background

### Gmail Push Notification Model

Gmail's push system uses Google Cloud Pub/Sub:

1. We call `users.watch()` per user, specifying a Pub/Sub topic
2. Gmail publishes a message to that topic whenever the mailbox changes
3. The Pub/Sub message contains only `emailAddress` and `historyId` — no email content
4. We call `users.history.list(startHistoryId=...)` to get the actual changes
5. For each new message ID, we call `users.messages.get()` to fetch the content

Watches expire after 7 days. Push is best-effort — notifications can be delayed, duplicated, or dropped. This is documented behavior, not an edge case.

### Scale Context

- ~100 coordinator accounts
- ~5,000 emails/day across all coordinators (~50/coordinator/day)
- ~200 new messages/hour

At this scale, Gmail API quotas are not a concern. A 60-second poll across 100 users costs ~100 `history.list` calls/minute at 2 quota units each = 200 units/minute, well below the 250 units/second per-user burst limit.

### Lessons from PR #4

| Problem | How This RFC Avoids It |
| ------- | ---------------------- |
| Unauthenticated webhook — anyone could POST crafted Pub/Sub messages | OIDC bearer token verification from day one |
| Tight coupling — workers.py imported agent engine, classifier, scheduler | Pipeline lives in `api/gmail/`, fires a protocol-based hook, imports nothing outside gmail |
| Wasted API calls — fetched metadata then immediately fetched full message | Single `messages.get(format=full)` per message, no metadata pre-fetch |
| 49-file PR with bundled concerns | This RFC is infrastructure only. Agent logic is a separate RFC and PR |
| Global mutable state race (`_action_url`) | No global mutable state. Worker context passed via arq's `ctx` dict |

## Proposed Design

### Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Google Cloud                                │
│                                                                 │
│  Gmail ──push──► Pub/Sub Topic ──push──► POST /webhook/gmail    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                                │
                                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                   LRP Backend (services/api)                    │
│                                                                 │
│  Webhook Handler (gmail/webhook.py)                             │
│       │  Verify OIDC token                                      │
│       │  Parse emailAddress + historyId                         │
│       └──► Enqueue arq job: process_gmail_push                  │
│                                                                 │
│  arq Worker (gmail/workers.py)                                  │
│       │                                                         │
│       ├── 1. history.list(startHistoryId) → new message IDs     │
│       ├── 2. Dedup check (processed_messages table)             │
│       ├── 3. messages.get(format=full) for each new message     │
│       ├── 4. Classify direction + type (deterministic)          │
│       ├── 5. Build EmailEvent                                   │
│       └── 6. Fire hook: await hook.on_email(event)              │
│                                                                 │
│  Cron Jobs                                                      │
│       ├── poll_gmail_history    (every 60s — fallback)          │
│       ├── renew_gmail_watches   (every 6h)                      │
│       └── cleanup_processed     (daily)                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Detailed Design

#### 1. Gmail Client Extensions

**File:** `services/api/src/api/gmail/client.py`

Four new methods on the existing `GmailClient`, using the established `_exec()` pattern:

```python
async def watch(self, user_email: str, topic_name: str) -> dict:
    """Register Pub/Sub push notifications for a mailbox.

    Returns {"historyId": "...", "expiration": "..."}
    Watches all labels (not filtered) — scheduling replies may be
    auto-archived, labeled, or appear in Sent.
    """

async def stop_watch(self, user_email: str) -> None:
    """Unregister push notifications for a mailbox."""

async def history_list(
    self,
    user_email: str,
    start_history_id: str,
    history_types: list[str] | None = None,
) -> dict:
    """Fetch mailbox changes since a history ID.

    Returns {"history": [...], "historyId": "latest_id"}
    On 404 (expired historyId), raises GmailNotFoundError.
    """

async def get_profile(self, user_email: str) -> dict:
    """Fetch user profile. Used to get initial historyId.

    Returns {"emailAddress": "...", "historyId": "...", ...}
    """
```

**Why no `get_message_metadata()`?** PR #4 fetched metadata (headers only) and then immediately fetched the full message — a wasted API call. We skip the metadata step entirely and fetch `format=full` once. The pre-filter (scheduling relevance) is the agent's concern, not this pipeline's.

#### 2. Database Schema

**File:** `services/api/migrations/0003_gmail_push_pipeline.py`

```sql
-- Extend gmail_tokens with push pipeline state
ALTER TABLE gmail_tokens
    ADD COLUMN last_history_id TEXT,
    ADD COLUMN watch_expiry TIMESTAMPTZ;

-- Idempotent deduplication: track which messages we've already processed
CREATE TABLE processed_messages (
    gmail_message_id    TEXT PRIMARY KEY,
    coordinator_email   TEXT NOT NULL,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_processed_messages_cleanup
    ON processed_messages (processed_at);
```

**`last_history_id`**: The cursor for incremental history sync. Advanced after each successful `history.list()` call. If it becomes stale (>30 days without polling for a user), `history.list()` returns 404 — we re-baseline via `get_profile()`.

**`watch_expiry`**: When the current Pub/Sub watch expires. Used by the renewal cron to prioritize coordinators whose watches are expiring soon.

**`processed_messages`**: Keyed by Gmail's globally unique message ID. Both push and poll paths check this before processing. The `processed_at` index supports the daily cleanup job (delete rows older than 30 days).

#### 3. SQL Queries

**File:** `services/api/queries/gmail_push.sql`

```sql
-- name: get_history_id(user_email)$
SELECT last_history_id FROM gmail_tokens WHERE user_email = :user_email;

-- name: update_history_id(user_email, last_history_id)!
UPDATE gmail_tokens
SET last_history_id = :last_history_id, updated_at = now()
WHERE user_email = :user_email;

-- name: update_watch_state(user_email, last_history_id, watch_expiry)!
UPDATE gmail_tokens
SET last_history_id = :last_history_id,
    watch_expiry = :watch_expiry,
    updated_at = now()
WHERE user_email = :user_email;

-- name: get_all_watched_emails
SELECT user_email FROM gmail_tokens;

-- name: is_message_processed(gmail_message_id)$
SELECT EXISTS(
    SELECT 1 FROM processed_messages WHERE gmail_message_id = :gmail_message_id
) AS is_processed;

-- name: mark_message_processed(gmail_message_id, coordinator_email)!
INSERT INTO processed_messages (gmail_message_id, coordinator_email)
VALUES (:gmail_message_id, :coordinator_email)
ON CONFLICT (gmail_message_id) DO NOTHING;

-- name: cleanup_old_processed_messages!
DELETE FROM processed_messages WHERE processed_at < now() - INTERVAL '30 days';
```

#### 4. Email Event Model and Hook Interface

**File:** `services/api/src/api/gmail/hooks.py`

```python
class MessageDirection(str, Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"

class MessageType(str, Enum):
    NEW_THREAD = "new_thread"
    REPLY = "reply"
    FORWARD = "forward"

class EmailEvent(BaseModel):
    """Structured event fired for every processed email."""
    message: Message
    coordinator_email: str
    direction: MessageDirection
    message_type: MessageType
    new_participants: list[EmailAddress]  # non-empty only for forwards

class EmailHook(Protocol):
    """Interface for email event consumers."""
    async def on_email(self, event: EmailEvent) -> None: ...

class LoggingHook:
    """Default hook — logs every event. Replaced by agent in production."""
    async def on_email(self, event: EmailEvent) -> None:
        logger.info(
            "email_event direction=%s type=%s thread=%s subject=%s",
            event.direction.value,
            event.message_type.value,
            event.message.thread_id,
            event.message.subject,
        )
```

**Direction detection:**

```python
def classify_direction(message: Message, coordinator_email: str) -> MessageDirection:
    if message.from_.email.lower() == coordinator_email.lower():
        return MessageDirection.OUTGOING
    return MessageDirection.INCOMING
```

**Forward detection (participant-set diff):**

```python
def classify_message_type(
    message: Message,
    prior_messages: list[Message],
) -> tuple[MessageType, list[EmailAddress]]:
    """Classify a message as new thread, reply, or forward.

    A forward is defined as: a message that adds at least one recipient
    not seen in any prior message's from/to/cc fields.
    """
    if not prior_messages:
        return MessageType.NEW_THREAD, []

    # Build cumulative participant set from all prior messages
    seen: set[str] = set()
    for msg in prior_messages:
        seen.add(msg.from_.email.lower())
        for addr in msg.to + msg.cc:
            seen.add(addr.email.lower())

    # Check current message recipients for new participants
    current_recipients = message.to + message.cc
    new_participants = [
        addr for addr in current_recipients
        if addr.email.lower() not in seen
    ]

    if new_participants:
        return MessageType.FORWARD, new_participants

    # Has In-Reply-To or References header → reply
    if message.message_id_header:
        return MessageType.REPLY, []

    return MessageType.REPLY, []
```

**Why not subject-line heuristics?** PR #4's classifier struggled with forwards because it tried to detect "Fwd:" prefixes — a convention that varies across email clients (Gmail, Outlook, Apple Mail all do it differently). The participant-set diff is deterministic: if a message adds a recipient who wasn't on any prior message in the thread, it's a forward. This handles the exact case that tripped up PR #4 (coordinator forwards thread to recruiter).

**Edge cases:**
- Coordinator adds themselves to CC → not a new participant (they're already in the thread as sender)
- Gmail threads unrelated emails by subject similarity → false thread grouping is a Gmail limitation, not a forward-detection problem. We process what Gmail gives us.
- Reply-all that adds someone from a distribution list → correctly classified as forward (new participant appeared)

#### 5. Webhook Endpoint

**File:** `services/api/src/api/gmail/webhook.py`

```python
@webhook_router.post("/webhook/gmail")
async def gmail_webhook(request: Request) -> Response:
    """Receive Gmail Pub/Sub push notifications.

    1. Verify OIDC bearer token (Google-signed)
    2. Parse emailAddress + historyId from Pub/Sub data
    3. Validate coordinator has authorized the app
    4. Enqueue arq job for background processing
    5. Always return 200 (prevents Pub/Sub retries)
    """
```

**OIDC verification** — the critical security fix from PR #4:

```python
async def verify_pubsub_token(request: Request) -> None:
    """Verify the OIDC token Google attaches to Pub/Sub push messages.

    Google Cloud Pub/Sub signs push messages with a service account's
    OIDC token. We verify against Google's public keys.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = auth_header[7:]
    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=EXPECTED_AUDIENCE,
        )
        # Verify the token was issued by the expected service account
        if claims.get("email") != PUBSUB_SERVICE_ACCOUNT:
            raise HTTPException(status_code=403, detail="Unexpected sender")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token")
```

The webhook always returns 200, even on processing errors. Pub/Sub interprets non-2xx responses as delivery failures and retries with exponential backoff, which would create duplicate work. Errors are logged and the fallback poll catches anything the push missed.

#### 6. Background Workers

**File:** `services/api/src/api/gmail/workers.py`

**Push handler:**

```python
async def process_gmail_push(
    ctx: dict, coordinator_email: str, history_id: str
) -> None:
    """Process a Gmail push notification.

    Uses stored history_id as the cursor (more reliable than the push
    notification's history_id, which may be stale if multiple pushes
    arrive out of order).
    """
```

**Shared processing logic:**

```python
async def _process_history(
    ctx: dict, coordinator_email: str, start_history_id: str
) -> None:
    """Core processing loop shared by push and poll paths.

    1. history.list(startHistoryId) → list of new message IDs
    2. For each message ID:
       a. Skip if already in processed_messages (idempotent)
       b. Mark as processed
       c. Fetch full message via messages.get(format=full)
       d. Fetch thread for forward detection context
       e. Classify direction and type
       f. Build EmailEvent and fire hook
    3. Update stored last_history_id
    """
```

**Thread fetch optimization:** To classify forwards, we need prior messages in the thread. We fetch the full thread once per thread ID, not once per message. If multiple messages arrive in the same thread (e.g., a rapid back-and-forth), we process them sequentially and reuse the thread context.

**Debounce:** A Redis lock (`debounce:{thread_id}`, 60s TTL, NX) prevents redundant processing when a rapid burst of messages arrives in the same thread. The first message in the burst gets processed; subsequent ones are skipped and caught by the next poll cycle.

**Cron jobs:**

| Job | Interval | Purpose |
| --- | -------- | ------- |
| `poll_gmail_history` | 60 seconds | Fallback poll for dropped push notifications |
| `renew_gmail_watches` | 6 hours | Re-register Pub/Sub watches before 7-day expiry |
| `cleanup_processed_messages` | Daily | Delete dedup records older than 30 days |

**arq WorkerSettings:**

```python
class WorkerSettings:
    functions = [process_gmail_push]
    cron_jobs = [
        cron(poll_gmail_history, second=0),          # every 60s
        cron(renew_gmail_watches, hour={0,6,12,18}), # every 6h
        cron(cleanup_processed_messages, hour=3),     # 3am daily
    ]
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_jobs = 50
    job_timeout = 120
```

#### 7. App Wiring

**File:** `services/api/src/api/main.py`

Changes to the existing lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing db pool and gmail client setup ...

    # Redis for arq job queue
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = await create_pool(RedisSettings.from_dsn(redis_url))
    app.state.redis = redis

    # Email hook — default is logging, replaced by agent in production
    app.state.email_hook = LoggingHook()

    yield

    await pool.close()
    redis.close()
```

Mount webhook router:

```python
from api.gmail.webhook import webhook_router
app.include_router(webhook_router)
```

#### 8. Dev Scripts

**`scripts/dev-worker.sh`** — runs the arq worker process:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../services/api"
uv run python -m arq api.gmail.workers.WorkerSettings
```

**`scripts/dev-all.sh`** — add worker alongside API:

```bash
./scripts/dev-worker.sh &
WORKER_PID=$!
```

### TokenStore Extensions

**File:** `services/api/src/api/gmail/auth.py`

New methods on the existing `TokenStore`, matching the inline SQL pattern used by existing methods:

```python
async def get_history_id(self, user_email: str) -> str | None:
    """Load the last-processed Gmail history ID for incremental sync."""

async def update_history_id(self, user_email: str, history_id: str) -> None:
    """Advance the history cursor after successful sync."""

async def update_watch_state(
    self, user_email: str, history_id: str, watch_expiry: datetime
) -> None:
    """Update both history cursor and watch expiration."""

async def get_all_watched_emails(self) -> list[str]:
    """List all coordinator emails with stored tokens."""
```

## Reliability and Error Handling

### Dual-Path Guarantee (Push + Poll)

The 30-second SLA is met by the push path (~2-10s typical). The 60-second poll is the safety net:

| Scenario | Push | Poll | Worst-case latency |
| -------- | ---- | ---- | ------------------ |
| Normal operation | Fires ~5s | Fires ~60s | ~5s (push wins) |
| Push dropped | — | Fires ~60s | ~60s |
| Service restart | — | Fires after startup + next poll | ~90s |
| Gmail API outage | Fails | Fails | Recovers when API returns |

After a service restart, the poll job runs within its first 60-second cycle. It picks up from the stored `last_history_id` and processes everything that was missed.

### History ID Expiry

If a coordinator's stored `last_history_id` is older than ~30 days (user was inactive), `history.list()` returns HTTP 404. Recovery:

1. Call `get_profile()` to get the current `historyId`
2. Store it as the new `last_history_id`
3. Log a warning — we may have missed messages during the gap

This is acceptable because: (a) an inactive coordinator has no active scheduling threads, and (b) the PRD permits eventual consistency.

### Idempotent Processing

Both push and poll paths converge on `_process_history()`, which checks `processed_messages` before processing each message. The dedup key is Gmail's `message.id`, which is globally unique and stable.

Mark-then-process order: we insert into `processed_messages` **before** firing the hook. This means if the hook fails, the message is marked as processed and won't be retried. This is the at-most-once semantic. If at-least-once is needed later (retrying failed hooks), we can add a `status` column to `processed_messages` — but for now, at-most-once plus a logged error is sufficient.

### Rate Limits

| Operation | Per-unit cost | Volume at our scale | Total/hour |
| --------- | ------------- | ------------------- | ---------- |
| `history.list()` (poll, 100 users × 60/hr) | 2 units | 6,000 calls | 12,000 units |
| `messages.get()` (~200 new messages/hr) | 5 units | 200 calls | 1,000 units |
| `threads.get()` (~100 unique threads/hr) | 10 units | 100 calls | 1,000 units |
| `watch()` (renewal, 100 users × 4/day) | 100 units | ~17 calls/hr | 1,700 units |
| **Total** | | | **~15,700 units/hr** |

Per-user burst limit: 250 units/second. Per-project daily limit: 1,000,000,000 units. We're at ~0.004% of the per-user limit and ~0.04% of the daily project limit.

## Security

### Webhook Authentication

The Pub/Sub push subscription is configured with OIDC authentication. Google signs each push message with a bearer token issued by a Google-managed service account. Our webhook verifies this token against Google's public keys before processing.

This was PR #4's most critical security bug — the webhook had no authentication, meaning anyone could POST crafted messages to trigger processing of any coordinator's mailbox.

### No Email Content Storage

The pipeline stores:
- Message IDs (for deduplication, 30-day TTL)
- History IDs (cursor state per coordinator)
- Watch expiry timestamps

It does **not** store email subjects, bodies, sender addresses, or any email content. The `EmailEvent` is constructed in memory, fired to the hook, and discarded. If the hook consumer needs to persist anything, that's its responsibility.

### Existing OAuth Token Security

The encrypted refresh token storage in `TokenStore` (Fernet encryption, Postgres storage) is unchanged. The new `last_history_id` and `watch_expiry` columns contain non-sensitive operational state.

### Configurable Scope Checking and Re-Auth

The push pipeline itself does not require new OAuth scopes — `gmail.modify` covers `users.watch()`, `users.history.list()`, and `users.messages.get()`. However, the application will need additional scopes as we add Calendar, Encore, and other integrations. Rather than hardcoding scopes as a Python constant, we introduce a configurable scope system that detects stale grants and prompts re-authorization.

**Design:**

1. **`.env` declares required scopes:**

```
REQUIRED_SCOPES=https://www.googleapis.com/auth/gmail.modify
```

Comma-separated. As features are added (Calendar, Encore), new scopes are appended here. The Python constant `SCOPES` in `auth.py` reads from this env var instead of being hardcoded.

2. **`TokenStore` validates scope coverage on credential load:**

When `load_credentials()` is called, compare the user's stored scopes (already in the `scopes TEXT[]` column of `gmail_tokens`) against `REQUIRED_SCOPES`. If any required scope is missing from the stored grant, raise a new `GmailScopeError` (subclass of `GmailAuthError`) instead of returning credentials.

```python
required = set(REQUIRED_SCOPES)
granted = set(row[1])  # scopes column from gmail_tokens
missing = required - granted
if missing:
    raise GmailScopeError(
        f"User {user_email} is missing scopes: {missing}. Re-authorization required.",
        missing_scopes=list(missing),
    )
```

3. **Callers handle `GmailScopeError` as a re-auth prompt:**

- **Add-on sidebar:** Catches `GmailScopeError` and renders the existing auth-required card, directing the coordinator to re-authorize.
- **Push pipeline workers:** Catches `GmailScopeError`, logs a warning, and skips processing for that coordinator. The coordinator will be prompted to re-auth next time they open the sidebar.

4. **OAuth consent flow uses configured scopes:**

The `oauth_start` endpoint already reads `SCOPES` from `auth.py`. By changing that constant to read from `.env`, the consent screen automatically requests the current required scopes when a coordinator re-authorizes.

**Why this matters now:** Google OAuth refresh tokens are scoped to the grant that created them. When we add Calendar scopes later, every coordinator will need to re-consent. Having the system detect this automatically (instead of silently failing with 403s) prevents a class of "the agent stopped working" support tickets.

## File Summary

| File | Status | Purpose |
| ---- | ------ | ------- |
| `services/api/migrations/0003_gmail_push_pipeline.py` | New | Schema for history tracking + dedup |
| `services/api/queries/gmail_push.sql` | New | aiosql queries for push pipeline tables |
| `services/api/src/api/gmail/client.py` | Modified | Add `watch`, `stop_watch`, `history_list`, `get_profile` |
| `services/api/src/api/gmail/auth.py` | Modified | Add history/watch state methods, scope validation, `GmailScopeError` |
| `services/api/src/api/gmail/exceptions.py` | Modified | Add `GmailScopeError` exception |
| `services/api/src/api/gmail/hooks.py` | New | EmailEvent model, EmailHook protocol, classification logic |
| `services/api/src/api/gmail/webhook.py` | New | Authenticated Pub/Sub webhook endpoint |
| `services/api/src/api/gmail/workers.py` | New | arq worker functions (push, poll, renewal, cleanup) |
| `services/api/src/api/main.py` | Modified | Redis init, webhook router mount, default hook |
| `scripts/dev-worker.sh` | New | Worker process dev script |
| `scripts/dev-all.sh` | Modified | Add worker process |

## Open Questions

1. **Thread fetch scope:** For forward detection, we need prior messages in the thread. Should we fetch the full thread for every new message, or only when the message has recipients not in our coordinator list? Full thread fetch is simpler but costs 10 quota units per new message.

2. **Hook error semantics:** If the hook raises an exception, should we retry? Current design: at-most-once (log error, move on). Alternative: add a retry queue with exponential backoff. At-most-once is simpler and acceptable for logging; retry may be needed when the agent is the consumer.

3. **Watch registration timing:** When should we first call `watch()` for a coordinator? Options: (a) immediately after OAuth consent, (b) lazily on first poll cycle, (c) manually via an admin endpoint. Option (a) is most responsive but requires wiring into the OAuth callback.
