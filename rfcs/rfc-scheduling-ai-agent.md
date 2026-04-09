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

- **G1: The agent reacts to incoming emails without coordinator action.** When an email arrives in a coordinator's mailbox that relates to a scheduling loop, the agent classifies it and prepares a suggested next action — before the coordinator opens it.
- **G2: The agent suggests the next action with a draft email.** For each active loop, the sidebar shows the agent's recommended action and a pre-composed draft. The coordinator sends, edits, or rejects.
- **G3: The agent classifies emails into scheduling-relevant categories.** New interview requests, availability responses, time confirmations, reschedules, cancellations, and irrelevant/informational messages are distinguished automatically.
- **G4: The agent handles the happy path end-to-end with minimal coordinator input.** For straightforward scheduling flows (request → availability → confirmation → scheduled), the coordinator's role reduces to reviewing and approving drafts.
- **G5: The agent asks the coordinator when uncertain.** Ambiguous situations (unclear candidate, conflicting availability, unusual client requests) surface as questions in the sidebar, not guesses.
- **G6: The agent never misses an email.** Even if push notifications are delayed or lost, a background sync guarantees every email is eventually processed.

## Non-Goals

- **Autonomous email sending.** The agent drafts; the coordinator sends. The agent does not have a "send email" tool. _Rationale:_ core product constraint from the approved proposal. Trust is built incrementally.
- **Calendar event creation.** Creating Google Calendar events and Zoom links remains manual or is deferred to a future phase. _Rationale:_ calendar integration is a separate concern with its own complexity (Zoom API, timezone handling, multi-party invitations).
- **Encore/ATS integration.** Reading from or writing to Encore is out of scope. _Rationale:_ Cluein integration has not been built yet. The agent will rely on its own contacts database and coordinator input.
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

### Why Classification Matters

The agent's email classification is not just a label — it's the bridge between unstructured email and structured state machine transitions. Classification buys us three things:

1. **Deterministic action mapping.** Each classification maps to a specific suggested action and stage transition. Without classification, the agent would have to infer both "what happened" and "what to do" in one step. Separating them makes each step auditable and debuggable.

2. **Metrics and observability.** Classification gives us a countable, trackable signal. We can measure: how many new requests per week, what's the median time from `availability_response` to `time_confirmation`, which clients are slow to confirm. These directly feed the MTTI and IFR metrics from the approved proposal.

3. **Filtering.** Only ~40% of coordinator email traffic is scheduling-related. Classification is the gate that prevents the agent from wasting cycles on the 60% that's irrelevant. A fast, cheap classification step avoids expensive draft generation for emails that don't need it.

## Proposed Design

### Overview

Three new components layer onto the existing system:

1. **Gmail Push Pipeline** — Pub/Sub watch on coordinator inboxes, delivering new-message notifications to a webhook endpoint. Backed by a history-sync fallback that guarantees no email is missed.
2. **Agent Reasoning Engine** — LLM-powered classification and draft generation, with Langfuse observability, structured evals, and a multi-provider abstraction layer (Anthropic primary, OpenAI fallback).
3. **Suggestion Model** — Persisted agent suggestions (next action + optional draft) that the sidebar UI reads and displays within the existing two-tab structure.

No existing API endpoints change. The sidebar's Actions tab integrates agent suggestions alongside manual next-action items. The Status Board tab continues to display loop statuses unchanged.

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
│       ├── 2. Scheduling relevance filter (fast, cheap)      │
│       │      └── Skip ~60% of non-scheduling emails         │
│       │                                                     │
│       ├── 3. Match to loop (loop_email_threads + contacts)  │
│       │                                                     │
│       ├── 4. Agent Engine (LLM)                             │
│       │      ├── Classify email                             │
│       │      ├── Determine next action                      │
│       │      └── Draft response (if applicable)             │
│       │                                                     │
│       └── 5. Persist suggestion (agent_suggestions table)   │
│                                                             │
│  Add-on Endpoints (existing)                                │
│       ├── on-message: reads suggestion, renders card        │
│       └── homepage: Actions tab shows pending suggestions   │
│                                                             │
│  Background Workers (arq/Redis)                             │
│       ├── History sync (guaranteed delivery fallback)       │
│       ├── Watch renewal (every 6 hours)                     │
│       └── Failed job retry (exponential backoff)            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Detailed Design

#### 1. Gmail Push Notification Pipeline

**Setup:** For each coordinator who authorizes the add-on, we call `gmail.users.watch()` to register a Pub/Sub watch on their mailbox. Watches cover **all labels** (not just INBOX), because scheduling replies may be auto-archived by Gmail filters, labeled, or appear in Sent mail threads.

**Watch registration:**

```python
async def register_gmail_watch(user_email: str) -> WatchResponse:
    """Register Pub/Sub push notifications for a coordinator's mailbox."""
    return await gmail_client.watch(
        user_email=user_email,
        topic_name="projects/{project}/topics/gmail-push",
        # No label_ids filter — watch everything
    )
```

Watches expire after 7 days. A background task (arq/Redis) renews them every 6 hours to ensure we never miss the expiry window.

**Webhook endpoint:**

```
POST /webhook/gmail
```

Receives Pub/Sub push messages. Each message contains `emailAddress` and `historyId` — not the email content itself. The handler:

1. Validates the Pub/Sub message authenticity (Google-signed OIDC token in `Authorization` header)
2. Calls `gmail.users.history.list()` to fetch new message IDs since the last known `historyId`
3. For each new message, enqueues an arq job for processing (not inline — see rate limiting below)

**Guaranteed delivery — never missing an email:**

Push notifications are best-effort: they can be delayed, duplicated, or dropped. To guarantee every email is eventually processed, we use a two-layer approach:

1. **Push (primary):** Pub/Sub delivers near-real-time. The webhook enqueues processing jobs immediately.
2. **Pull (fallback):** A background worker runs every 5 minutes per coordinator, calling `gmail.users.history.list()` with the stored `historyId`. Any messages found that weren't already processed get enqueued. This catches anything push missed.

**Idempotent processing:** Every message is tracked by `gmail_message_id` in a `processed_messages` table (or Redis set with TTL). If a message has already been processed (by push or pull), the job short-circuits. This makes it safe for both push and pull to fire for the same message.

```sql
CREATE TABLE processed_messages (
    gmail_message_id TEXT PRIMARY KEY,
    coordinator_email TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- TTL cleanup: delete rows older than 30 days via periodic job
```

**History tracking:** We store the last-processed `historyId` per coordinator. On every successful history sync (push or pull), we advance the stored `historyId` to the latest value. If the stored `historyId` becomes invalid (Gmail returns 404), we fall back to a full sync of the last 24 hours.

**Deleted messages and threads:** If a coordinator deletes a message or thread from Gmail after the agent has processed it, the suggestion and any loop data remain intact in our database. Our system is the source of truth for scheduling state, not Gmail. The agent records the relevant email content at processing time, so subsequent Gmail deletions don't cause data loss. If a coordinator opens a deleted thread's loop in the sidebar, the loop state is still accurate even though the original email is gone.

#### Gmail API Rate Limiting and Error Handling

The Gmail API has per-user rate limits (250 quota units/second per user) and per-project limits. Our pipeline hits the API in three places: history.list (on every push notification), messages.get (for each new message), and threads.get (for full thread context). For a coordinator receiving 50 emails/hour, that's roughly 150 API calls/hour — well within limits.

**Rate limit strategy:**

| Concern | Mitigation |
| ------- | ---------- |
| Burst push notifications (rapid-fire thread replies) | Deduplicate by `gmail_thread_id` with a Redis debounce lock: at most one agent run per thread per 60 seconds. Later messages in the burst are deferred, then caught by the next pull sync. |
| Per-user quota exhaustion | arq job queue with per-user concurrency limit of 1. Jobs for the same coordinator serialize. |
| Per-project quota exhaustion | Exponential backoff on 429 responses (1s → 2s → 4s → ... → 60s cap). Jobs re-enqueue themselves on transient failures. |
| Gmail API outage | Jobs fail and re-enqueue with backoff. Pull sync catches missed messages when API recovers. |
| Stale OAuth tokens | On 401, attempt a single token refresh. If refresh fails, mark coordinator as needing re-auth and surface in sidebar. |

**Error budget:** We log all Gmail API errors to Langfuse as trace events (see Observability section). If error rate exceeds 5% of requests over a 15-minute window, an alert fires.

#### 2. Scheduling Relevance Pre-Filter

Only ~40% of coordinator email traffic is scheduling-related. Running the full agent (thread fetch + LLM call) on every email would waste API quota and LLM spend. We add a fast, cheap filter before the agent runs.

**Pre-filter implementation:**

The pre-filter is a lightweight classification step that determines whether an email is worth processing. It runs before fetching the full thread or calling the LLM.

```python
async def is_scheduling_relevant(message: gmail.Message, coordinator: Coordinator) -> bool:
    """Fast check: is this email likely about scheduling?"""

    # 1. Known thread — already linked to a loop
    loop = await loop_service.find_loop_by_thread(message.thread_id)
    if loop is not None:
        return True

    # 2. Known sender — email from a contact in our contacts DB
    sender_email = message.sender_email
    if await has_known_contact(sender_email):
        return True

    # 3. Keyword heuristic — check subject + first 500 chars of body
    scheduling_signals = [
        "interview", "schedule", "availability", "round 1", "round 2",
        "meet", "candidate", "time slot", "reschedule", "cancel",
    ]
    text = f"{message.subject} {message.body_preview}".lower()
    if any(signal in text for signal in scheduling_signals):
        return True

    return False
```

This is deliberately conservative (high recall, moderate precision). It's better to pass a non-scheduling email to the agent (which will classify it as `unrelated` and take no action) than to miss a real scheduling email. The LLM call is the cost gate, not the pre-filter.

**Metrics:** We track pre-filter pass rate in Langfuse. If it's consistently above 60%, the keyword list needs tuning.

#### 3. Agent Reasoning Engine

The agent is the core of this system. We follow the Agent Development Life Cycle (ADLC) methodology: define scope and success criteria upfront, instrument everything from day one, and iterate through a build → eval → improve flywheel.

##### 3.1 Scope and Intent

The agent is a **single-shot reasoning function**: it takes a fully assembled context (email + thread + loop state + contacts) and returns a structured suggestion (classification + action + optional draft). It does not maintain conversation state, does not call tools, and does not iterate. Every invocation is independent.

**What it does:**
- Classifies incoming emails into scheduling categories
- Determines the next action based on the loop's state machine
- Drafts response emails matching LRP's communication style
- Identifies when it's uncertain and asks the coordinator

**What it does NOT do:**
- Send emails (no send capability in its tools)
- Create or modify loops directly (suggestions are proposals, not actions)
- Access external systems (no Encore, no Calendar — all context is pre-assembled)
- Make judgment calls about candidate quality, interview outcomes, or hiring decisions

##### 3.2 LLM Abstraction Layer

The agent must not be tightly coupled to a single LLM provider. We introduce a thin abstraction that supports Anthropic as primary and OpenAI as fallback, ensuring the agent stays operational even during provider outages.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class LLMProvider(ABC):
    @abstractmethod
    async def complete(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResponse: ...


class AnthropicProvider(LLMProvider):
    """Primary provider: Claude via Anthropic API."""

    async def complete(self, system, user, max_tokens, temperature) -> LLMResponse:
        response = await self.client.messages.create(
            model=self.model,  # e.g. "claude-sonnet-4-20250514"
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return LLMResponse(
            content=response.content[0].text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=...,
        )


class OpenAIProvider(LLMProvider):
    """Fallback provider: GPT-4o via OpenAI API."""

    async def complete(self, system, user, max_tokens, temperature) -> LLMResponse:
        response = await self.client.chat.completions.create(
            model=self.model,  # e.g. "gpt-4o"
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return LLMResponse(...)


class LLMRouter:
    """Routes to primary provider, falls back on failure."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider | None = None):
        self.primary = primary
        self.fallback = fallback

    async def complete(self, **kwargs) -> LLMResponse:
        try:
            return await self.primary.complete(**kwargs)
        except Exception as e:
            if self.fallback is None:
                raise
            logger.warning(f"Primary LLM failed ({e}), falling back to {self.fallback}")
            return await self.fallback.complete(**kwargs)
```

**Configuration:** The `LLMRouter` is initialized at app startup from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). If only one key is present, no fallback is configured. The sidebar shows "Agent unavailable — manual workflow active" when both providers are down.

##### 3.3 Agent Context and Output

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
```

**Output:**

```python
@dataclass
class AgentSuggestion:
    # What the agent thinks happened
    classification: EmailClassification

    # What the agent recommends doing next
    suggested_action: SuggestedAction

    # Questions for the coordinator (if uncertain)
    questions: list[str]

    # Agent's reasoning (for transparency/debugging)
    reasoning: str

    # Confidence score (0-1)
    confidence: float


@dataclass
class DraftEmail:
    """Separate from the suggestion — not all suggestions include a draft."""
    to: list[str]
    subject: str
    body: str
    in_reply_to: str | None  # Gmail message ID for threading
```

**Email classification categories:**

| Classification             | Description                                       | Example                                                 |
| -------------------------- | ------------------------------------------------- | ------------------------------------------------------- |
| `new_interview_request`    | Client asking to interview a candidate             | "I'd like to meet John Smith for a first round"         |
| `availability_response`    | Recruiter/candidate providing available times      | "John is free Tuesday 2-4pm and Thursday 10am-12pm"     |
| `time_confirmation`        | Client confirming a specific interview time        | "Tuesday 2pm works for us"                              |
| `reschedule_request`       | Any party asking to move a scheduled interview     | "Something came up, can we move to next week?"          |
| `cancellation`             | Interview being cancelled                          | "We've decided not to proceed with this candidate"      |
| `follow_up_needed`         | A reply that needs coordinator attention but isn't a clear state transition | "Let me check and get back to you" |
| `informational`            | No action needed                                   | "Thanks for confirming!"                                |
| `unrelated`                | Not about scheduling                               | "Can you update the JD for this role?"                  |

**Suggested actions:**

| Action                     | Stage Transition                              | Includes Draft? |
| -------------------------- | --------------------------------------------- | --------------- |
| `draft_to_recruiter`       | NEW → AWAITING_CANDIDATE                      | Yes             |
| `draft_to_client`          | AWAITING_CANDIDATE → AWAITING_CLIENT          | Yes             |
| `draft_confirmation`       | AWAITING_CLIENT → SCHEDULED                   | Yes             |
| `draft_follow_up`          | (no transition)                                | Yes             |
| `request_new_availability` | AWAITING_CLIENT → AWAITING_CANDIDATE          | Yes             |
| `mark_cold`                | Any → COLD                                    | No              |
| `create_loop`              | (new loop)                                    | No (pre-fills loop form) |
| `ask_coordinator`          | (no transition)                                | No              |
| `no_action`                | (no transition)                                | No              |

##### 3.4 Agent Implementation

The agent is a single LLM call with a carefully constructed system prompt. The prompt includes:
- The scheduling workflow rules (state machine, who talks to whom, LRP conventions)
- The current loop state and event history
- The email thread content
- Output format instructions (structured JSON)

```python
@observe()  # Langfuse trace
async def run_agent(ctx: AgentContext, llm: LLMRouter) -> AgentSuggestion:
    """Run the scheduling agent on a new email."""
    system_prompt = build_system_prompt(ctx)
    user_prompt = build_user_prompt(ctx)

    response = await llm.complete(
        system=system_prompt,
        user=user_prompt,
        max_tokens=2048,
        temperature=0.2,
    )

    suggestion = parse_agent_response(response.content)

    # Log to Langfuse for observability
    langfuse_context.update_current_observation(
        input={"classification": ctx.new_message.subject},
        output={"action": suggestion.suggested_action, "confidence": suggestion.confidence},
        metadata={"model": response.model, "tokens": response.input_tokens + response.output_tokens},
    )

    return suggestion
```

**Prompt management:** System prompts are versioned in Langfuse's prompt management system, not hardcoded. This allows prompt iteration without code deploys:

```python
prompt_template = langfuse.get_prompt("scheduling-agent-system", label="production")
system_prompt = prompt_template.compile(
    state_machine_rules=STATE_MACHINE_DOCS,
    loop_context=format_loop_context(ctx.loop),
    event_history=format_events(ctx.events),
)
```

The system prompt is the core intellectual property — it encodes the scheduling workflow, LRP's communication style, and the rules for when to act vs. when to ask. Prompt iteration happens through the eval flywheel (see section 3.6), not ad-hoc edits.

**Why a single call, not a multi-turn agent loop:**

The scheduling domain is well-structured enough that a single reasoning step suffices. The agent doesn't need to "explore" — it classifies an email, checks the state machine, and drafts. Multi-turn adds latency (multiple API round-trips), complexity (error handling for each tool), and unpredictability (the agent might take unexpected actions). If the agent is uncertain, it says so (via `ask_coordinator`), rather than attempting autonomous recovery. We can always add tool use later if single-shot proves insufficient for edge cases.

##### 3.5 Observability (Langfuse)

Every agent run produces a Langfuse trace. This is non-negotiable — we cannot improve what we cannot measure.

**What we trace:**

| Observation | Type | Data |
| ----------- | ---- | ---- |
| `agent-run` | Trace | email subject, thread ID, loop ID, coordinator |
| `pre-filter` | Span | pass/fail, reason, latency |
| `context-assembly` | Span | loop state, actor count, thread message count |
| `llm-call` | Generation | model, prompt, response, tokens, latency, cost |
| `response-parsing` | Span | success/fail, classification, action |
| `suggestion-persist` | Span | suggestion ID, status |

**Scores attached to each trace:**

- `classification_accepted` (binary) — did the coordinator accept the classification?
- `draft_accepted` (binary) — was the draft sent as-is, edited, or rejected?
- `action_correct` (binary) — was the suggested action the right one? (set when coordinator overrides)

These scores are written back to Langfuse when the coordinator interacts with the suggestion in the sidebar. They feed the eval flywheel.

**Dashboard alerts:**

- Classification rejection rate > 20% over 1 hour → prompt may need tuning
- Draft edit rate > 50% over 1 day → drafting quality degraded
- LLM error rate > 5% over 15 minutes → provider issue
- Average latency > 5 seconds → performance regression

##### 3.6 Eval Strategy (ADLC Flywheel)

Following the Agent Development Life Cycle, we define evals before building:

**Success KPIs:**

| KPI | Target | How Measured |
| --- | ------ | ------------ |
| Classification accuracy | > 90% | Supervisor eval against labeled dataset |
| Draft acceptance rate (sent as-is) | > 60% | Production Langfuse scores |
| Agent suggestion acceptance rate | > 80% | Production Langfuse scores |
| Time-to-suggestion | < 10 seconds | Langfuse trace latency |
| Cost per suggestion | < $0.05 | Langfuse token tracking |

**Eval dataset:** Start with 50 real email threads from coordinator archives (anonymized), each labeled with expected classification, expected action, and a reference draft. Expand to 200+ as production data flows in.

**Unsupervised evals (run continuously on production traces):**

```json
{
    "name": "classification_relevance",
    "type": "llm_judge",
    "prompt": "Email: {{email_subject}}\nClassification: {{classification}}\n\nIs this classification correct for the email? Answer PASS or FAIL, then explain.",
    "model": "claude-haiku"
}
```

**Supervised evals (run as experiments when prompt changes):**

1. Run new prompt against the full eval dataset
2. Compare classification accuracy, draft quality (LLM-judged similarity to reference), and action correctness
3. Only promote if scores improve without regressions

**Flywheel cadence:** Weekly review of production scores → identify failure patterns → adjust prompt → run supervised eval → promote or revert.

##### 3.7 Guardrails

**Input guardrails (before LLM call):**

- **PII stripping:** Remove SSNs, credit card numbers, or other sensitive data from email content before sending to the LLM. Coordinator emails shouldn't contain these, but defense in depth.
- **Content length gate:** If the email thread exceeds 50 messages or 100KB of text, truncate to the most recent 20 messages. Log the truncation in Langfuse.

**Output guardrails (after LLM response):**

- **Schema validation:** The LLM response must parse into a valid `AgentSuggestion`. If parsing fails, log the raw response and skip (no suggestion created).
- **Action validation:** The suggested action must be valid for the current stage state. If the agent suggests `draft_to_client` but the stage is `NEW`, reject the suggestion and log the mismatch.
- **Draft recipient validation:** If a draft is included, the recipients must be known contacts (in our contacts DB) or the coordinator themselves. Drafts to unknown recipients are flagged for coordinator review.

Every guardrail trigger is logged as a Langfuse event. We monitor guardrail fire rate as a health metric.

#### 4. Suggestion Persistence

Agent suggestions are stored in a dedicated table. Drafts are stored in a separate table since not all suggestions include a draft.

```sql
CREATE TABLE agent_suggestions (
    id               TEXT PRIMARY KEY,        -- asg_<nanoid>
    loop_id          TEXT REFERENCES loops(id),
    stage_id         TEXT REFERENCES stages(id),
    -- What triggered this suggestion
    gmail_message_id TEXT NOT NULL,
    gmail_thread_id  TEXT NOT NULL,
    -- Agent output
    classification   TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    questions        TEXT[],             -- questions for coordinator (if ask_coordinator)
    reasoning        TEXT,
    confidence       REAL NOT NULL,
    -- For create_loop suggestions: pre-filled fields
    prefilled_data   JSONB,             -- {candidate_name, client_name, client_email, ...}
    -- Coordinator disposition
    status           TEXT NOT NULL DEFAULT 'pending',
        -- pending: not yet reviewed
        -- accepted: coordinator approved (draft sent or action taken)
        -- edited: coordinator modified and sent
        -- rejected: coordinator dismissed
    coordinator_feedback TEXT,           -- optional correction notes
    -- Timestamps
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE suggestion_drafts (
    id               TEXT PRIMARY KEY,        -- sgd_<nanoid>
    suggestion_id    TEXT NOT NULL REFERENCES agent_suggestions(id),
    draft_to         TEXT[] NOT NULL,         -- recipient emails
    draft_subject    TEXT NOT NULL,
    draft_body       TEXT NOT NULL,
    in_reply_to      TEXT,                    -- Gmail message ID for threading
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_suggestions_loop ON agent_suggestions(loop_id, created_at DESC);
CREATE INDEX idx_suggestions_thread ON agent_suggestions(gmail_thread_id, created_at DESC);
CREATE INDEX idx_suggestions_pending ON agent_suggestions(status) WHERE status = 'pending';
CREATE INDEX idx_drafts_suggestion ON suggestion_drafts(suggestion_id);
```

**Design decisions:**

- **The most recent suggestion wins.** When the agent runs again on the same thread (e.g., new message arrives), the old suggestion is naturally superseded — we always query `ORDER BY created_at DESC LIMIT 1` for a given loop or thread. Old suggestions remain in the table for audit and training data, but the sidebar only displays the latest one. No explicit `superseded` status needed.
- **Drafts in a separate table.** Not all suggestions include a draft (e.g., `mark_cold`, `ask_coordinator`, `create_loop`, `no_action`). Normalizing drafts into their own table avoids nullable columns and makes it clear which suggestions have associated email content.
- **`status` tracks coordinator disposition.** This is the feedback loop: when the coordinator edits a draft before sending, we record `edited` + `coordinator_feedback`. These signals feed back into Langfuse as eval scores.
- **`loop_id` can be NULL.** For `create_loop` suggestions (new interview request detected), the loop doesn't exist yet. The `prefilled_data` JSONB field holds whatever the agent could extract from the email (candidate name, client name/email, subject).
- **`confidence` for UI differentiation.** Low-confidence suggestions (< 0.7) are visually distinguished in the sidebar with a warning indicator, prompting more careful coordinator review.

#### 5. Sidebar Integration

The sidebar retains its existing two-tab structure. Agent suggestions integrate into the existing UI rather than creating new views.

**Tab 1: Actions**

This tab shows everything the coordinator needs to act on, combining agent suggestions with manual next-action items:

```
┌─────────────────────────────────┐
│ LRP Scheduling Agent            │
│ [Actions] [Status Board]        │
├─────────────────────────────────┤
│                                 │
│ 🤖 AGENT SUGGESTIONS (3)       │
│ ┌─────────────────────────────┐ │
│ │ Smith → Acme Capital        │ │
│ │ Send availability to client │ │
│ │ [Approve] [Edit] [Dismiss]  │ │
│ ├─────────────────────────────┤ │
│ │ Jones → Vertex Fund         │ │
│ │ New interview request       │ │
│ │ [Create Loop] [Dismiss]     │ │
│ ├─────────────────────────────┤ │
│ │ ⚠️ Chen → BluePeak          │ │
│ │ Agent needs clarification   │ │
│ │ "Is this Round 2 or a new   │ │
│ │  process?"                  │ │
│ │ [Answer]                    │ │
│ └─────────────────────────────┘ │
│                                 │
│ ⏳ WAITING (8)                  │
│ ┌─────────────────────────────┐ │
│ │ Park → Summit Partners      │ │
│ │ Awaiting candidate avail.   │ │
│ │ 2 days                      │ │
│ └─────────────────────────────┘ │
│                                 │
│ ✓ AGENT UNAVAILABLE            │
│   (if applicable — banner)     │
│   Manual workflow active.      │
│   The agent will resume when   │
│   the service recovers.        │
└─────────────────────────────────┘
```

In the ideal case, the coordinator opens the Actions tab and approves multiple suggestions in quick succession — one click each for straightforward cases.

**Tab 2: Status Board**

Unchanged from the current implementation. Shows every active loop grouped by status (action needed, waiting, scheduled, complete, cold). No agent suggestions here — this tab is pure loop state.

**Contextual view (on-message trigger):**

When the coordinator opens a specific email, the sidebar shows the suggestion for that thread (if one exists):

```
┌─────────────────────────────────┐
│ Smith → Acme Capital            │
│ Round 1 · AWAITING_CANDIDATE    │
│ ─────────────────────────────── │
│                                 │
│ 🤖 Agent Suggestion             │
│ Availability response from      │
│ recruiter — send to client      │
│                                 │
│ ┌─────────────────────────────┐ │
│ │ Draft Email                 │ │
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
│ ▸ Agent reasoning (collapsed)  │
└─────────────────────────────────┘
```

**Action buttons:**

| Button       | Behavior                                                              |
| ------------ | --------------------------------------------------------------------- |
| **Send / Approve** | Creates Gmail draft then sends it. Records `accepted` on suggestion. Advances stage. |
| **Edit**     | Opens compose card (existing) pre-filled with agent's draft. After send, records `edited` + captures diff as feedback. |
| **Reject / Dismiss** | Marks suggestion `rejected`. Optionally captures feedback text. |
| **Answer** (for `ask_coordinator`) | Free-text response field. Agent re-runs with coordinator's answer as additional context. |
| **Create Loop** (for `create_loop`) | Opens create-loop form pre-filled with agent's extracted data. |

**Agent unavailable:** When both LLM providers are down, the Actions tab shows a non-intrusive banner: "Agent unavailable — manual workflow active." All manual controls (create loop, send email, advance stage) continue to work normally. The coordinator reverts to their pre-agent workflow. When the agent recovers, it processes any missed emails via the pull sync and suggestions appear.

#### 6. Unmatched Emails (New Loop Detection)

When the agent receives a notification for an email that doesn't match any existing loop (no `loop_email_threads` match for the Gmail thread ID), it attempts to identify whether this is a new interview request.

Since we don't currently have Encore/Cluein integration, the agent relies on:

1. **The email content itself** — parse for candidate names, interview mentions, round numbers, client context
2. **The contacts database** — match sender/recipient emails against known contacts (recruiters, client managers, client contacts) built up from previous loops
3. **Coordinator's address book** — the sender's name and email can identify the client contact

**Pre-fill strategy for `create_loop` suggestions:**

| Field | Source | Confidence |
| ----- | ------ | ---------- |
| Client contact | Sender email matched to `client_contacts` table | High (if match found) |
| Client company | From matched client contact record | High |
| Candidate name | Parsed from email body by agent | Medium |
| Recruiter | Matched from `contacts` table by candidate/company context | Low — often left blank for coordinator to fill |
| Title | Generated by agent (e.g., "Smith → Acme Capital") | Medium |

The recruiter is typically *not* identifiable from the initial client email. The agent pre-fills everything it can and leaves the recruiter field for the coordinator. This is fine — the coordinator knows which recruiter owns each candidate.

### Error Handling

| Failure Mode                        | Handling                                                      |
| ----------------------------------- | ------------------------------------------------------------- |
| Gmail push delayed or missing       | Pull sync every 5 min catches missed messages                 |
| Gmail API rate limit (429)          | Exponential backoff, re-enqueue job via arq                   |
| Gmail API outage                    | Jobs re-enqueue with backoff; pull sync recovers on restore   |
| OAuth token expired                 | Auto-refresh; if fails, mark coordinator for re-auth          |
| LLM primary provider down           | Automatic fallback to OpenAI; if both down, skip agent, surface banner |
| LLM returns unparseable response    | Schema validation guardrail catches it; log to Langfuse; no suggestion created |
| Agent misclassifies email           | Coordinator rejects; feedback stored as Langfuse score        |
| Agent suggests invalid transition   | Action validation guardrail catches it; log mismatch          |
| Duplicate push notifications        | Idempotent: `processed_messages` table deduplicates           |
| Coordinator deletes email/thread    | No impact — our DB is source of truth; loop state preserved   |
| Thread matches multiple loops       | Present all matches to coordinator; agent picks most likely    |
| History ID becomes invalid          | Fall back to full sync of last 24 hours                       |

### Data Flow: End-to-End Example

**Scenario:** Client emails coordinator asking to interview a candidate.

```
1. Client sends email to coordinator@lrp.com
2. Gmail Pub/Sub pushes notification to POST /webhook/gmail
3. Webhook handler:
   a. Validate Pub/Sub message
   b. Fetch history since last historyId → find new message ID
   c. Check processed_messages → not seen before → proceed
   d. Enqueue arq job: process_new_message(coordinator, message_id)
4. arq worker picks up job:
   a. Fetch message via Gmail API
   b. Pre-filter: subject contains "interview" → relevant
   c. No matching loop found (new thread)
   d. Search contacts DB: sender jhirsch@acmecap.com matches
      client contact "Jeff Hirsch, Acme Capital"
   e. Run agent with context (message + sender match)
   f. Agent classifies: new_interview_request
   g. Agent extracts: candidate="John Smith"
   h. Agent cannot identify recruiter → leaves blank
   i. Agent suggests: create_loop
      prefilled_data: {candidate_name: "John Smith",
                       client_contact_id: "cli_abc123",
                       title: "Smith → Acme Capital"}
   j. Suggestion persisted with status=pending
   k. Langfuse trace recorded with all observations
5. Coordinator opens the client's email in Gmail
6. Sidebar on-message trigger:
   a. No loop found for this thread
   b. Pending suggestion found for this thread
   c. Renders: "New interview request detected"
   d. Shows pre-filled create-loop form (candidate + client filled,
      recruiter blank for coordinator to select)
7. Coordinator reviews:
   a. Confirms candidate name, client contact
   b. Selects recruiter from autocomplete (Sarah Jones)
   c. Clicks "Create Loop"
8. Backend:
   a. Creates loop + first stage (NEW)
   b. Links email thread to loop
   c. Marks suggestion as accepted
   d. Records all events in loop_events
   e. Agent auto-runs on the new loop: suggests draft_to_recruiter
   f. Draft: "Hi Sarah, Acme Capital would like to schedule a
      first round with John Smith. Could you please send over
      his availability for next week?"
   g. New suggestion with draft appears in sidebar
9. Coordinator clicks "Send As-Is"
10. Backend:
    a. Sends email to recruiter via Gmail API
    b. Advances stage: NEW → AWAITING_CANDIDATE
    c. Marks suggestion as accepted
    d. Langfuse score: classification_accepted=true, draft_accepted=true
```

## Alternatives Considered

### Polling instead of Pub/Sub

We could poll each coordinator's inbox on a schedule (e.g., every 60 seconds) instead of using push notifications.

**Rejected because:** Polling N coordinator inboxes every minute wastes API quota when nothing has changed, doesn't scale to more coordinators, and adds 0–60 seconds of latency. Pub/Sub is near-real-time and costs nothing when idle. We do use polling as a fallback (every 5 minutes), but not as the primary mechanism.

### Multi-turn agent with tool use

We could give the agent tools (read email, search contacts, query database) and let it iterate autonomously until it reaches a suggestion.

**Rejected because:** The scheduling domain is structured enough that a single reasoning step with pre-assembled context works. Tool use adds latency (multiple API round-trips), complexity (error handling for each tool), and unpredictability (the agent might take unexpected actions). We can always add tool use later if single-shot proves insufficient for edge cases.

### Storing drafts as Gmail drafts immediately

Instead of storing draft text in `suggestion_drafts`, we could create an actual Gmail draft via `GmailClient.create_draft()` the moment the agent runs.

**Rejected because:** Creating Gmail drafts for every incoming email would clutter the coordinator's drafts folder. Most suggestions are accepted as-is, so there's no benefit to having the draft in Gmail before the coordinator reviews it. We create the Gmail draft only when the coordinator clicks "Send" or "Edit."

### Agent runs in the sidebar request path

Instead of a webhook-triggered background process, the agent could run when the coordinator opens the sidebar (in the `on-message` handler).

**Rejected because:** The issue explicitly requires webhook-based processing. Running the agent in the request path also means 2-5 second latency in the sidebar load, which degrades UX. Background processing means the suggestion is ready before the coordinator opens the email.

### Single LLM provider (Anthropic only)

We could hardcode the Anthropic SDK and skip the abstraction layer.

**Rejected because:** Provider outages are real, and the agent being down means coordinators lose the "suggestion ready before you open the email" benefit entirely. An OpenAI fallback keeps the agent operational during Anthropic outages. The abstraction layer is thin (one interface, two implementations) and pays for itself the first time there's a provider incident.

## Implementation Plan

### Phase 1: Gmail Push Pipeline + Guaranteed Delivery

1. Add `last_history_id` and `watch_expiry` columns to `gmail_tokens`
2. Create `processed_messages` table migration
3. Implement `GmailClient.watch()`, `GmailClient.history_list()` methods
4. Create `POST /webhook/gmail` endpoint with Pub/Sub message validation
5. Register watches during OAuth callback
6. Add arq background workers: watch renewal (6-hourly), history sync (5-minute fallback)
7. Implement idempotent message processing with deduplication

### Phase 2: LLM Abstraction + Agent Engine

1. Create `src/api/agent/` module with `llm.py`, `engine.py`, `prompts.py`, `models.py`
2. Implement `LLMProvider` abstraction with Anthropic and OpenAI implementations
3. Implement `LLMRouter` with fallback logic
4. Define `AgentContext` and `AgentSuggestion` Pydantic models
5. Implement scheduling relevance pre-filter
6. Build initial system prompt encoding scheduling workflow rules
7. Implement `run_agent()` function with structured output parsing
8. Add Langfuse tracing to all agent operations
9. Set up prompt management in Langfuse (staging/production labels)
10. Write integration tests with fixture emails

### Phase 3: Suggestion Persistence + Webhook Wiring

1. Create `agent_suggestions` and `suggestion_drafts` table migrations
2. Implement suggestion CRUD in new `AgentService`
3. Wire webhook handler: fetch message → pre-filter → build context → run agent → persist suggestion
4. Add Redis-based thread debounce (60-second per-thread window)
5. Add per-user job concurrency limits in arq
6. Implement guardrails (schema validation, action validation, recipient validation)

### Phase 4: Sidebar Integration

1. Extend `on-message` handler to fetch latest pending suggestion for thread
2. Extend homepage Actions tab to show pending suggestions
3. Build suggestion card UI (classification, draft, action buttons)
4. Implement Send/Edit/Reject/Answer action handlers
5. Record suggestion disposition + write Langfuse scores
6. Handle `create_loop` suggestions (pre-filled loop creation flow)
7. Add "Agent unavailable" banner when both providers are down

### Phase 5: Eval Flywheel + Production Hardening

1. Build initial eval dataset (50 labeled email threads)
2. Configure Langfuse unsupervised evals (classification relevance, draft quality)
3. Set up supervised eval pipeline (run against dataset on prompt changes)
4. Configure Langfuse dashboard alerts (rejection rate, edit rate, error rate, latency)
5. Run first eval cycle and iterate on prompt
6. Add input guardrails (PII stripping, content length gate)
7. Load test with simulated email volume

## Open Questions

1. **Which LLM models?** Sonnet is fast and cheap for classification; Opus is better for nuanced draft writing. Should we use a cheaper model (Haiku/GPT-4o-mini) for the pre-filter + classification step and a more capable model (Sonnet/GPT-4o) for draft generation, or a single model for both? Two models adds complexity but could halve LLM costs.

2. **What's the fallback when the agent is down?** Resolved: surface "Agent unavailable — manual workflow active" banner in the Actions tab. Coordinators revert to their existing manual workflow. When the agent recovers, pull sync catches missed emails and suggestions appear. Should we also queue failed agent jobs for retry, or just let the next pull sync handle it?

3. **How do we seed the eval dataset?** We need 50+ real scheduling email threads for the initial eval dataset. Options: (a) coordinators export threads manually, (b) we use the Gmail API to pull recent threads matching scheduling keywords, (c) we synthesize realistic test emails. Option (b) is fastest but needs coordinator consent.
