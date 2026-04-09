# RFC: Scheduling AI Agent

| Field         | Value                                 |
| ------------- | ------------------------------------- |
| **Author(s)** | Kinematic Labs                        |
| **Status**    | Draft                                 |
| **Created**   | 2026-04-09                            |
| **Updated**   | 2026-04-09                            |
| **Reviewers** | LRP Engineering, LRP Coordinator team |
| **Decider**   | Nim Sadeh                             |
| **Issue**     | #2                                    |

## Context and Scope

The manual scheduling loop infrastructure is in place: coordinators can create loops, track stages, send emails, and manage contacts through the Gmail sidebar add-on. Every action is captured in an event-sourced log. The data model and state machine work.

Now we build the brain: an AI agent that reads incoming emails, classifies them, determines the next action in a scheduling loop, and drafts a response for the coordinator to approve. The coordinator still approves every outbound email — the agent accelerates the coordinator, it doesn't replace them.

This RFC covers the agent's architecture, email ingestion pipeline, reasoning system, and integration with the existing add-on UI. It intentionally reuses the existing API surface and data model, adding only what's necessary for autonomous email classification and draft generation.

## Goals

- **G1: The agent reacts to incoming emails without coordinator action.** When an email arrives in a coordinator's inbox that relates to a scheduling loop, the agent classifies it and prepares a suggested next action — before the coordinator opens it.
- **G2: The agent suggests the next action with a draft email.** For each active loop, the sidebar shows the agent's recommended action and a pre-composed draft. The coordinator sends, edits, or rejects.
- **G3: The agent classifies emails into scheduling-relevant categories.** New interview requests, availability responses, time confirmations, reschedules, cancellations, and irrelevant/informational messages are distinguished automatically.
- **G4: The agent handles the happy path end-to-end with minimal coordinator input.** For straightforward scheduling flows (request → availability → confirmation → scheduled), the coordinator's role reduces to reviewing and approving drafts.
- **G5: The agent asks the coordinator when uncertain.** Ambiguous situations (unclear candidate, conflicting availability, unusual client requests) surface as questions in the sidebar, not guesses.

## Non-Goals

- **Autonomous email sending.** The agent drafts; the coordinator sends. The agent does not have a "send email" tool. _Rationale:_ core product constraint from the approved proposal. Trust is built incrementally.
- **Calendar event creation.** Creating Google Calendar events and Zoom links remains manual or is deferred to a future phase. _Rationale:_ calendar integration is a separate concern with its own complexity (Zoom API, timezone handling, multi-party invitations).
- **Encore/ATS updates.** Writing to Encore after interviews is out of scope. _Rationale:_ Cluein integration is a separate workstream.
- **Learning or fine-tuning.** The agent uses structured client preferences (stored as data), not model fine-tuning. _Rationale:_ approved proposal specifies structured rules, not model changes.
- **Multi-coordinator handoff.** The agent assumes one coordinator per loop. _Rationale:_ LRP coordinators have clear client ownership; handoff is rare and manual.

## Background

### How the Agent Fits Into the Existing System

The current system is coordinator-driven: the coordinator opens an email, creates/updates loops through the sidebar, and sends emails manually. The event log records everything.

The agent adds a reactive layer:

```
Today (manual):
  Email arrives → Coordinator reads → Coordinator decides → Coordinator acts → Sidebar records

With agent:
  Email arrives → Agent classifies → Agent drafts → Sidebar shows suggestion → Coordinator approves
```

The key difference: the agent does its work _before_ the coordinator opens the email. When the coordinator does open it, the sidebar already has a suggested action and draft ready.

### Why Gmail Push Notifications

The issue specifies: "You need to read the emails... via webhook — the coordinator should not have to open a message or the app for you to take action."

Gmail's push notification system (Pub/Sub watch) sends a notification to our backend whenever a new message arrives in a watched mailbox. This is the trigger for the agent — not the coordinator opening the sidebar.

## Proposed Design

### Overview

Three new components layer onto the existing system:

1. **Gmail Push Pipeline** — Pub/Sub watch on coordinator inboxes, delivering new-message notifications to a webhook endpoint.
2. **Agent Reasoning Engine** — Claude-powered classification and draft generation, operating on email content + loop context + client preferences.
3. **Suggestion Model** — Persisted agent suggestions (next action + draft) that the sidebar UI reads and displays.

No existing API endpoints change. The add-on sidebar gains a new display mode: "agent suggestion" cards that show the recommended action and draft. The existing manual controls remain available as fallback.

### System Context Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Google Cloud                            │
│                                                             │
│  Gmail ──push──► Pub/Sub Topic ──push──► /webhook/gmail     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   LRP Backend (services/api)                │
│                                                             │
│  Webhook Handler                                            │
│       │                                                     │
│       ├── 1. Fetch new messages (GmailClient)               │
│       │                                                     │
│       ├── 2. Match to loop (loop_email_threads)             │
│       │                                                     │
│       ├── 3. Agent Engine (Claude)                          │
│       │      ├── Classify email                             │
│       │      ├── Determine next action                      │
│       │      └── Draft response                             │
│       │                                                     │
│       └── 4. Persist suggestion (agent_suggestions table)   │
│                                                             │
│  Add-on Endpoints (existing)                                │
│       └── on-message: reads suggestion, renders card        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Detailed Design

#### 1. Gmail Push Notification Pipeline

**Setup:** For each coordinator who authorizes the add-on, we call `gmail.users.watch()` to register a Pub/Sub watch on their inbox. The watch sends notifications to a Cloud Pub/Sub topic that pushes to our webhook endpoint.

**Watch registration:**

```python
# Called during OAuth callback or on coordinator first use
async def register_gmail_watch(user_email: str) -> WatchResponse:
    """Register Pub/Sub push notifications for a coordinator's inbox."""
    return await gmail_client.watch(
        user_email=user_email,
        topic_name="projects/{project}/topics/gmail-push",
        label_ids=["INBOX"],
    )
```

Watches expire after 7 days. A background task (Redis-based via arq) renews them before expiry.

**Webhook endpoint:**

```
POST /webhook/gmail
```

Receives Pub/Sub push messages. Each message contains `emailAddress` and `historyId` — not the email content itself. The handler:

1. Validates the Pub/Sub message authenticity
2. Calls `gmail.users.history.list()` to fetch new message IDs since the last known `historyId`
3. For each new message, fetches full content via `GmailClient.get_message()`
4. Passes each message to the agent reasoning engine

**History tracking:** We store the last-processed `historyId` per coordinator in a new column on the `gmail_tokens` table. This ensures we don't re-process messages after restarts.

**Rate limiting:** Gmail push notifications can arrive in bursts (e.g., a thread with rapid replies). We deduplicate by `gmail_thread_id` + `message_id` and process at most one agent run per thread per 30-second window, using a Redis-based debounce lock.

#### 2. Agent Reasoning Engine

The agent is a stateless function that takes context and returns a structured suggestion. It does not maintain conversation state across calls — every invocation gets the full context it needs.

**Input context (assembled by the webhook handler):**

```python
@dataclass
class AgentContext:
    # The new email that triggered this run
    new_message: gmail.Message

    # Full thread history (all messages in the thread)
    thread_messages: list[gmail.Message]

    # The matched loop (if any) with full state
    loop: Loop | None

    # Event history for this loop
    events: list[LoopEvent]

    # Known actors
    coordinator: Coordinator
    recruiter: Contact | None
    client_contact: ClientContact | None
    candidate: Candidate | None

    # Client preferences (future: per-client rules)
    client_preferences: dict | None
```

**Output:**

```python
@dataclass
class AgentSuggestion:
    # What the agent thinks happened
    classification: EmailClassification

    # What the agent recommends doing next
    suggested_action: SuggestedAction

    # Draft email (if the action involves sending an email)
    draft: DraftEmail | None

    # Questions for the coordinator (if uncertain)
    questions: list[str]

    # Agent's reasoning (for transparency/debugging)
    reasoning: str

    # Confidence score (0-1)
    confidence: float
```

**Email classification categories:**

| Classification             | Description                                       | Example                                                 |
| -------------------------- | ------------------------------------------------- | ------------------------------------------------------- |
| `new_interview_request`    | Client asking to interview a candidate             | "I'd like to meet John Smith for a first round"         |
| `availability_response`    | Recruiter/candidate providing available times      | "John is free Tuesday 2-4pm and Thursday 10am-12pm"     |
| `time_confirmation`        | Client confirming a specific interview time        | "Tuesday 2pm works for us"                              |
| `reschedule_request`       | Any party asking to move a scheduled interview     | "Something came up, can we move to next week?"          |
| `cancellation`             | Interview being cancelled                          | "We've decided not to proceed with this candidate"      |
| `follow_up_needed`         | A reply that needs coordinator attention but isn't a clear state transition | "Let me check and get back to you"    |
| `informational`            | No action needed                                   | "Thanks for confirming!"                                |
| `unrelated`                | Not about scheduling                               | "Can you update the JD for this role?"                  |

**Suggested actions:**

| Action                     | Stage Transition                              | Draft Target     |
| -------------------------- | --------------------------------------------- | ---------------- |
| `draft_to_recruiter`       | NEW → AWAITING_CANDIDATE                      | Recruiter        |
| `draft_to_client`          | AWAITING_CANDIDATE → AWAITING_CLIENT          | Client contact   |
| `draft_confirmation`       | AWAITING_CLIENT → SCHEDULED                   | Recruiter + Client |
| `draft_follow_up`          | (no transition)                                | Whoever is blocking |
| `request_new_availability` | AWAITING_CLIENT → AWAITING_CANDIDATE          | Recruiter        |
| `mark_cold`                | Any → COLD                                    | None             |
| `create_loop`              | (new loop)                                    | None             |
| `ask_coordinator`          | (no transition)                                | None             |
| `no_action`                | (no transition)                                | None             |

**Agent implementation:**

The agent is a single Claude API call with a carefully constructed system prompt. The prompt includes:
- The scheduling workflow rules (state machine, who talks to whom)
- The current loop state and event history
- The email thread content
- Client-specific preferences (if any)
- Output format instructions (structured JSON)

```python
async def run_agent(ctx: AgentContext) -> AgentSuggestion:
    """Run the scheduling agent on a new email."""
    system_prompt = build_system_prompt(ctx)
    user_prompt = build_user_prompt(ctx)

    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return parse_agent_response(response)
```

The system prompt is the core intellectual property — it encodes the scheduling workflow, LRP's communication style, and the rules for when to act vs. when to ask. This is a prompt engineering challenge, not a model training challenge.

**Why a single call, not a multi-turn agent loop:**

The scheduling domain is well-structured enough that a single reasoning step suffices. The agent doesn't need to "explore" — it classifies an email, checks the state machine, and drafts. Multi-turn adds latency and complexity without clear benefit for this use case. If the agent is uncertain, it says so (via `ask_coordinator`), rather than attempting autonomous recovery.

#### 3. Suggestion Persistence

Agent suggestions are stored in a new table so the sidebar can read them without re-running the agent:

```sql
CREATE TABLE agent_suggestions (
    id          TEXT PRIMARY KEY,        -- asg_<nanoid>
    loop_id     TEXT REFERENCES loops(id),
    stage_id    TEXT REFERENCES stages(id),
    -- What triggered this suggestion
    gmail_message_id TEXT NOT NULL,
    gmail_thread_id  TEXT NOT NULL,
    -- Agent output
    classification   TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    draft_to         TEXT[],             -- recipient emails
    draft_subject    TEXT,
    draft_body       TEXT,
    questions        TEXT[],             -- questions for coordinator
    reasoning        TEXT,
    confidence       REAL NOT NULL,
    -- Coordinator disposition
    status           TEXT NOT NULL DEFAULT 'pending',
        -- pending: not yet reviewed
        -- accepted: coordinator approved (draft sent or action taken)
        -- edited: coordinator modified and sent
        -- rejected: coordinator dismissed
        -- superseded: new suggestion replaced this one
    coordinator_feedback TEXT,           -- optional correction notes
    -- Timestamps
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX idx_suggestions_loop ON agent_suggestions(loop_id, created_at DESC);
CREATE INDEX idx_suggestions_status ON agent_suggestions(status) WHERE status = 'pending';
CREATE INDEX idx_suggestions_thread ON agent_suggestions(gmail_thread_id);
```

**Design decisions:**

- **Suggestions are immutable once created.** If the agent runs again on the same thread (e.g., new message arrives), the old suggestion is marked `superseded` and a new one is created. This preserves the audit trail.
- **`status` tracks coordinator disposition.** This is the feedback loop: when the coordinator edits a draft before sending, we record `edited` + `coordinator_feedback`. Over time, patterns in feedback inform client preference rules.
- **`loop_id` can be NULL for `create_loop` suggestions.** When the agent sees a new interview request that doesn't match an existing loop, it suggests creating one — before the loop exists.
- **`confidence` is for future use.** Low-confidence suggestions could be visually distinguished in the sidebar (e.g., "Agent is unsure — please review carefully").

#### 4. Sidebar Integration

The existing add-on `on-message` handler already fetches the loop for the current thread. We extend it to also fetch the latest pending suggestion:

**Current flow:**
```
on-message → find loop by thread → render loop detail card
```

**New flow:**
```
on-message → find loop by thread → find pending suggestion → render suggestion card (with loop context)
```

**Suggestion card UI (Card v2 JSON):**

```
┌─────────────────────────────────┐
│ Agent Suggestion                │
│ ─────────────────────────────── │
│ Classification: Availability    │
│ response from recruiter         │
│                                 │
│ Suggested Action:               │
│ Send availability to client     │
│                                 │
│ ┌─────────────────────────────┐ │
│ │ Draft Email                 │ │
│ │                             │ │
│ │ To: jhirsch@acmecap.com    │ │
│ │ Subject: Re: John Smith...  │ │
│ │                             │ │
│ │ Hi Jeff,                    │ │
│ │                             │ │
│ │ John is available at the    │ │
│ │ following times:            │ │
│ │ - Tue 4/15, 2-4pm ET       │ │
│ │ - Thu 4/17, 10am-12pm ET   │ │
│ │                             │ │
│ │ Please let me know which    │ │
│ │ works best.                 │ │
│ │                             │ │
│ │ Best,                       │ │
│ │ [Coordinator]               │ │
│ └─────────────────────────────┘ │
│                                 │
│ [Send As-Is] [Edit] [Reject]   │
│                                 │
│ Agent reasoning:                │
│ ▸ Recruiter replied with 2     │
│   time windows for candidate.  │
│   Forwarding to client contact │
│   per standard workflow.       │
└─────────────────────────────────┘
```

**Action buttons:**

| Button       | Behavior                                                              |
| ------------ | --------------------------------------------------------------------- |
| **Send**     | Creates Gmail draft, then sends it. Records `accepted` on suggestion. Advances stage. |
| **Edit**     | Opens compose card (existing) pre-filled with agent's draft. After send, records `edited`. |
| **Reject**   | Marks suggestion `rejected`. Optionally captures feedback text.       |

When the agent suggests `ask_coordinator`, the card shows the agent's questions instead of a draft, with a free-text response field.

#### 5. Unmatched Emails (New Loop Detection)

When the agent receives a notification for an email that doesn't match any existing loop (no `loop_email_threads` match for the Gmail thread ID), it attempts to identify whether this is a new interview request:

1. **Parse the email** for scheduling signals (candidate names, interview mentions, client context)
2. **Search Encore** (via Cluein) for candidate and recruiter records matching names/emails in the thread
3. **If classified as `new_interview_request`:** create a suggestion with `suggested_action: create_loop`, pre-filling candidate name, client contact, and recruiter from the parsed email + Encore lookup
4. **If not a scheduling email:** classify as `unrelated` and take no action

The sidebar shows unmatched suggestions on the homepage status board under a new "New Requests" section.

#### 6. Watch Management

**Registration lifecycle:**

1. Coordinator completes OAuth → backend stores refresh token → calls `gmail.users.watch()`
2. Store `watch_expiry` timestamp alongside the token
3. Background worker (arq/Redis) runs hourly, renewing watches expiring within 24 hours
4. If coordinator revokes OAuth, the watch is automatically invalidated by Google

**New columns on `gmail_tokens`:**

```sql
ALTER TABLE gmail_tokens
    ADD COLUMN last_history_id TEXT,
    ADD COLUMN watch_expiry TIMESTAMPTZ;
```

**Webhook security:** Pub/Sub push messages are verified by checking the `Authorization` header contains a valid Google-signed OIDC token for our service account.

### Error Handling

| Failure Mode                        | Handling                                                      |
| ----------------------------------- | ------------------------------------------------------------- |
| Gmail push delayed or missing       | Hourly background sync fetches recent history as fallback     |
| Agent API call fails (rate limit)   | Retry with exponential backoff via arq job queue              |
| Agent returns unparseable response  | Log error, skip suggestion, surface "Agent unavailable" in sidebar |
| Agent misclassifies email           | Coordinator rejects suggestion, feedback stored for review    |
| Duplicate push notifications        | Deduplicate by message ID; skip if suggestion already exists  |
| Watch expires without renewal       | Caught by hourly renewal job; manual re-watch on next OAuth refresh |
| Thread matches multiple loops       | Present all matches to coordinator; agent picks most likely   |

### Data Flow: End-to-End Example

**Scenario:** Client emails coordinator asking to interview a candidate. Recruiter has been identified in Encore.

```
1. Client sends email to coordinator@lrp.com
2. Gmail Pub/Sub pushes notification to POST /webhook/gmail
3. Webhook handler:
   a. Fetch new message via Gmail API
   b. No matching loop found (new thread)
   c. Agent classifies as new_interview_request
   d. Agent extracts: candidate="John Smith", client="Acme Capital"
   e. Encore lookup finds recruiter: Sarah Jones (sarah@lrp.com)
   f. Agent suggests: create_loop + draft_to_recruiter
   g. Draft: "Hi Sarah, Acme Capital would like to schedule a
      first round with John Smith. Could you please send over
      his availability for next week?"
   h. Suggestion persisted with status=pending
4. Coordinator opens the client's email in Gmail
5. Sidebar loads:
   a. Fetches suggestion for this thread
   b. Renders: "New interview request detected"
   c. Shows pre-filled create-loop form + draft to recruiter
6. Coordinator reviews:
   a. Confirms loop details (client, candidate, recruiter)
   b. Reviews draft email to recruiter
   c. Clicks "Send"
7. Backend:
   a. Creates loop + first stage (NEW)
   b. Links email thread to loop
   c. Sends draft to recruiter via Gmail
   d. Advances stage: NEW → AWAITING_CANDIDATE
   e. Marks suggestion as accepted
   f. Records all events in loop_events
```

## Alternatives Considered

### Polling instead of Pub/Sub

We could poll each coordinator's inbox on a schedule (e.g., every 60 seconds) instead of using push notifications.

**Rejected because:** Polling N coordinator inboxes every minute wastes API quota when nothing has changed, doesn't scale to more coordinators, and adds 0–60 seconds of latency. Pub/Sub is near-real-time and costs nothing when idle.

### Multi-turn agent with tool use

We could give the agent tools (read email, search Encore, query database) and let it iterate autonomously until it reaches a suggestion.

**Rejected because:** The scheduling domain is structured enough that a single reasoning step with pre-assembled context works. Tool use adds latency (multiple API round-trips), complexity (error handling for each tool), and unpredictability (the agent might take unexpected actions). We can always add tool use later if single-shot proves insufficient for edge cases.

### Storing drafts as Gmail drafts immediately

Instead of storing draft text in `agent_suggestions`, we could create an actual Gmail draft via `GmailClient.create_draft()` the moment the agent runs.

**Rejected because:** Creating Gmail drafts for every incoming email would clutter the coordinator's drafts folder. Most suggestions are accepted as-is, so there's no benefit to having the draft in Gmail before the coordinator reviews it. We create the Gmail draft only when the coordinator clicks "Send" or "Edit."

### Agent runs in the sidebar request path

Instead of a webhook-triggered background process, the agent could run when the coordinator opens the sidebar (in the `on-message` handler).

**Rejected because:** The issue explicitly requires webhook-based processing. Running the agent in the request path also means 2-5 second latency in the sidebar load, which degrades UX. Background processing means the suggestion is ready before the coordinator opens the email.

## Implementation Plan

### Phase 1: Gmail Push Pipeline

1. Add `last_history_id` and `watch_expiry` columns to `gmail_tokens`
2. Implement `GmailClient.watch()` and `GmailClient.history_list()` methods
3. Create `POST /webhook/gmail` endpoint with Pub/Sub message validation
4. Register watches during OAuth callback
5. Add arq background task for watch renewal
6. Add arq background task for hourly history sync (fallback)

### Phase 2: Agent Engine

1. Create `src/api/agent/` module with `engine.py`, `prompts.py`, `models.py`
2. Define `AgentContext` and `AgentSuggestion` models
3. Build system prompt encoding scheduling workflow rules
4. Implement `run_agent()` function with Claude API call
5. Write integration tests with fixture emails

### Phase 3: Suggestion Persistence + Webhook Integration

1. Create `agent_suggestions` migration
2. Implement suggestion CRUD in `LoopService` (or new `AgentService`)
3. Wire webhook handler: fetch message → build context → run agent → persist suggestion
4. Add deduplication logic (Redis debounce + message ID check)

### Phase 4: Sidebar Integration

1. Extend `on-message` handler to fetch pending suggestions
2. Build suggestion card UI (classification, draft, action buttons)
3. Implement Send/Edit/Reject action handlers
4. Record suggestion disposition + feedback
5. Handle `create_loop` suggestions (pre-filled loop creation flow)

### Phase 5: Unmatched Email Handling

1. Implement new-thread detection in webhook handler
2. Add Encore/Cluein lookup for candidate/recruiter matching
3. Build "New Requests" section on status board homepage
4. Handle create-loop-from-suggestion flow

## Open Questions

1. **Which Claude model?** Sonnet is fast and cheap for classification; Opus is better for nuanced draft writing. Should we use Sonnet for classification and Opus for drafting, or a single model for both?

2. **Client preferences — what's the schema?** The approved proposal mentions learning client-specific workflows. What are the first preferences to capture? Possible starting set: preferred communication style (formal/informal), who sends calendar invites (coordinator vs. client), typical interview duration, timezone preferences.

3. **How should the status board show agent activity?** Options: (a) new "Agent Suggestions" group alongside action_needed/waiting/scheduled, (b) badges on existing loop entries indicating an agent suggestion is pending, (c) both.

4. **Should the agent process emails from all labels or just INBOX?** Some scheduling threads may be archived or labeled. Limiting to INBOX reduces noise but might miss important replies that were auto-archived by Gmail filters.

5. **What's the fallback when the agent is down?** The manual workflow still works — coordinators can create loops, send emails, and advance stages without the agent. Should we surface "Agent unavailable" in the sidebar, or silently fall back to manual mode?
