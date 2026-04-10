"""Prompt builders for the scheduling agent's classification and drafting steps.

Prompts are fetched from Langfuse at runtime (with in-process caching).
If Langfuse is unreachable, hardcoded fallbacks are used so the agent
never breaks due to a prompt-management outage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from api.agent.models import ClassificationResult, SuggestedAction
from api.scheduling.models import ALLOWED_TRANSITIONS, StageState

if TYPE_CHECKING:
    from api.agent.models import AgentContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Langfuse prompt fetching with fallback
# ---------------------------------------------------------------------------

# Prompt names in Langfuse
_CLASSIFIER_SYSTEM_NAME = "scheduling-classifier-system"
_CLASSIFIER_USER_NAME = "scheduling-classifier-user"
_DRAFTER_SYSTEM_NAME = "scheduling-drafter-system"
_DRAFTER_USER_NAME = "scheduling-drafter-user"


def _get_langfuse_prompt(name: str, fallback: str) -> str:
    """Fetch a prompt template from Langfuse, falling back to hardcoded default.

    Langfuse SDK caches prompts in-process (default TTL = 60s), so this
    is cheap after the first call.
    """
    try:
        from langfuse import get_client

        client = get_client()
        prompt = client.get_prompt(name, type="text")
        return prompt.prompt
    except Exception:
        logger.debug("Langfuse prompt '%s' unavailable, using fallback", name)
        return fallback


# ---------------------------------------------------------------------------
# Hardcoded fallbacks (kept in sync with Langfuse versions)
# These use Langfuse's {{variable}} syntax so compile() works identically.
# ---------------------------------------------------------------------------

_CLASSIFICATION_SYSTEM_FALLBACK = """\
You are an AI scheduling assistant for Long Ridge Partners (LRP), an executive \
search firm specializing in hedge funds and private equity. Your job is to \
classify incoming emails and suggest the next action for the scheduling \
coordinator.

## Scheduling workflow

Each interview request is tracked as a **Loop** that moves through stages:

### Stage states
{{stage_states}}

### Allowed transitions
{{transitions}}

## Email classifications

Classify the incoming email as one of:

- **new_interview_request** -- A client or internal stakeholder is requesting \
that an interview be scheduled.
- **availability_response** -- Someone (usually a recruiter relaying \
candidate info) is providing available time slots.
- **time_confirmation** -- Someone is confirming a specific interview time.
- **reschedule_request** -- Someone is asking to move an already-scheduled \
interview to a different time.
- **cancellation** -- Someone is cancelling an interview entirely.
- **follow_up_needed** -- The email indicates a stall or requires a nudge \
(e.g., no response for several days).
- **informational** -- The email contains useful context but requires no \
scheduling action (e.g., feedback, updates).
- **unrelated** -- The email is not related to interview scheduling.

## Suggested actions

After classifying, suggest the best next action:

- **draft_to_recruiter** -- Draft an email to the recruiter asking for \
candidate availability. Valid when stage is NEW.
- **draft_to_client** -- Draft an email to the client with proposed times. \
Valid when stage is AWAITING_CANDIDATE (availability received).
- **draft_confirmation** -- Draft a confirmation email to all parties. \
Valid when stage is AWAITING_CLIENT (client picked a time).
- **draft_follow_up** -- Draft a follow-up/nudge email to whoever hasn't \
responded. Valid when stage is NEW, AWAITING_CANDIDATE, or AWAITING_CLIENT.
- **request_new_availability** -- Ask for new time slots because the \
proposed ones were rejected. Valid when stage is AWAITING_CLIENT.
- **mark_cold** -- Mark the loop as stalled/cold. Valid in any active stage.
- **create_loop** -- Create a new scheduling loop for a new interview \
request. Only valid when no existing loop matches.
- **ask_coordinator** -- Escalate to the coordinator because the situation \
is ambiguous. Valid in any stage.
- **no_action** -- No action needed. Valid in any stage.

## Rules

1. If no loop exists and the email is a new interview request, suggest \
**create_loop**.
2. If a loop exists, the suggested action must be compatible with the \
current stage state (see allowed transitions above).
3. If you are unsure, suggest **ask_coordinator** with specific questions.
4. Always provide a confidence score between 0 and 1.

## Output format

Return ONLY valid JSON matching this schema (no extra text):

```json
{
  "classification": "<EmailClassification value>",
  "suggested_action": "<SuggestedAction value>",
  "confidence": <float 0-1>,
  "reasoning": "<brief explanation>",
  "questions": ["<optional questions for coordinator>"],
  "prefilled_data": {}  // only for create_loop: candidate_name, client_company, etc.
}
```
"""

_CLASSIFICATION_USER_FALLBACK = """\
## New email

**From:** {{from_name}} <{{from_email}}>
**Subject:** {{subject}}
**Date:** {{date}}

{{body}}

## Thread history

{{thread_history}}

## Current loop state

{{loop_state}}

## Recent events

{{events}}

---

Classify this email and suggest the next scheduling action. Return JSON only.\
"""

_DRAFT_SYSTEM_FALLBACK = """\
You are a professional email drafter for Long Ridge Partners (LRP), an \
executive search firm. You write emails on behalf of scheduling coordinators.

## Communication style

- **Professional and warm** -- courteous but not overly formal.
- **Concise** -- get to the point quickly. Coordinators are busy.
- **Scheduling-focused** -- every email should move the scheduling process \
forward.

## Role

You are drafting as the **coordinator** (an LRP employee). The coordinator \
manages interview logistics between clients and recruiters. The coordinator \
never emails candidates directly; the recruiter is the intermediary.

## Format guidelines

- Greeting: "Hi [First Name]," (warm, not "Dear").
- Keep paragraphs short (2-3 sentences max).
- When presenting time slots, use a clear bulleted list with dates, times, \
and timezones.
- Sign off with the coordinator's name: "Best,\\n[Coordinator Name]"
- For replies, do NOT repeat the full subject context; be brief.

## Output format

Return ONLY valid JSON matching this schema (no extra text):

```json
{
  "to": ["recipient@example.com"],
  "subject": "Subject line",
  "body": "Email body text",
  "in_reply_to": "<message-id or null>"
}
```
"""

_DRAFT_USER_FALLBACK = """\
## Classification result

- **Classification:** {{classification}}
- **Suggested action:** {{action}}
- **Reasoning:** {{reasoning}}

## Thread context

**Coordinator:** {{coordinator_name}} <{{coordinator_email}}>
{{recipient_info}}

## Thread history

{{thread_history}}

## Task

{{task_description}}

Sign off as: {{coordinator_name}}

If this is a reply, set in_reply_to to: {{in_reply_to}}

Return JSON only.\
"""

# ---------------------------------------------------------------------------
# Template compilation helper
# ---------------------------------------------------------------------------


def _compile_template(template: str, **kwargs: str) -> str:
    """Substitute {{variable}} placeholders in a prompt template.

    This mirrors Langfuse's compile() behaviour so that both
    Langfuse-fetched and fallback templates use the same syntax.
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace("{{" + key + "}}", value)
    return result


# ---------------------------------------------------------------------------
# Context formatting helpers
# ---------------------------------------------------------------------------


def _format_stage_states() -> str:
    lines = []
    for state in StageState:
        lines.append(f"- **{state.value}**")
    return "\n".join(lines)


def _format_transitions() -> str:
    lines = []
    for from_state, to_states in ALLOWED_TRANSITIONS.items():
        if to_states:
            targets = ", ".join(sorted(s.value for s in to_states))
            lines.append(f"- {from_state.value} -> {targets}")
        else:
            lines.append(f"- {from_state.value} -> (terminal)")
    return "\n".join(lines)


def _format_thread_history(ctx: AgentContext) -> str:
    if not ctx.thread_messages:
        return "(No prior messages in thread)"
    lines = []
    for msg in ctx.thread_messages:
        sender = msg.from_.email if msg.from_ else "unknown"
        snippet = msg.body_text[:300] if msg.body_text else msg.snippet
        lines.append(f"- [{msg.date.isoformat()}] {sender}: {snippet}")
    return "\n".join(lines)


def _format_loop_state(ctx: AgentContext) -> str:
    if ctx.loop is None:
        return "No matching loop found."
    loop = ctx.loop
    parts = [
        f"Loop: {loop.title} (id={loop.id})",
    ]
    if loop.stages:
        for stage in loop.stages:
            parts.append(f"  Stage '{stage.name}': state={stage.state.value}")
    else:
        parts.append("  (no stages)")
    if loop.candidate:
        parts.append(f"  Candidate: {loop.candidate.name}")
    if loop.client_contact:
        parts.append(
            f"  Client contact: {loop.client_contact.name} ({loop.client_contact.company})"
        )
    if loop.recruiter:
        parts.append(f"  Recruiter: {loop.recruiter.name} <{loop.recruiter.email}>")
    return "\n".join(parts)


def _format_events(ctx: AgentContext) -> str:
    if not ctx.events:
        return "(No recent events)"
    lines = []
    for event in ctx.events[-10:]:
        lines.append(f"- [{event.occurred_at.isoformat()}] {event.event_type.value}: {event.data}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_classification_prompt(ctx: AgentContext) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the classification step.

    Fetches prompt templates from Langfuse (with fallback to hardcoded).
    """
    system_template = _get_langfuse_prompt(_CLASSIFIER_SYSTEM_NAME, _CLASSIFICATION_SYSTEM_FALLBACK)
    user_template = _get_langfuse_prompt(_CLASSIFIER_USER_NAME, _CLASSIFICATION_USER_FALLBACK)

    system = _compile_template(
        system_template,
        stage_states=_format_stage_states(),
        transitions=_format_transitions(),
    )

    new_msg = ctx.new_message
    user = _compile_template(
        user_template,
        from_name=new_msg.from_.name or "",
        from_email=new_msg.from_.email,
        subject=new_msg.subject,
        date=new_msg.date.isoformat(),
        body=new_msg.body_text,
        thread_history=_format_thread_history(ctx),
        loop_state=_format_loop_state(ctx),
        events=_format_events(ctx),
    )

    return system, user


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------


def _describe_task(classification: ClassificationResult, ctx: AgentContext) -> str:
    action = classification.suggested_action
    if action == SuggestedAction.DRAFT_TO_RECRUITER:
        recruiter_name = ctx.recruiter.name if ctx.recruiter else "the recruiter"
        return (
            f"Draft an email to {recruiter_name} asking for the candidate's "
            f"availability for an interview."
        )
    if action == SuggestedAction.DRAFT_TO_CLIENT:
        client_name = ctx.client_contact.name if ctx.client_contact else "the client"
        return (
            f"Draft an email to {client_name} presenting the candidate's "
            f"available time slots for the interview."
        )
    if action == SuggestedAction.DRAFT_CONFIRMATION:
        return (
            "Draft a confirmation email to all parties with the confirmed "
            "interview time, date, and any logistics (Zoom link, location, etc.)."
        )
    if action == SuggestedAction.DRAFT_FOLLOW_UP:
        return "Draft a polite follow-up email nudging the recipient who has not yet responded."
    if action == SuggestedAction.REQUEST_NEW_AVAILABILITY:
        return (
            "Draft an email explaining that the previously proposed times "
            "don't work and requesting new availability."
        )
    return f"Draft an email for action: {action.value}"


def _format_recipient_info(classification: ClassificationResult, ctx: AgentContext) -> str:
    action = classification.suggested_action
    parts: list[str] = []
    if action in {
        SuggestedAction.DRAFT_TO_RECRUITER,
        SuggestedAction.REQUEST_NEW_AVAILABILITY,
    }:
        if ctx.recruiter:
            parts.append(f"**Recruiter (recipient):** {ctx.recruiter.name} <{ctx.recruiter.email}>")
    elif action == SuggestedAction.DRAFT_TO_CLIENT and ctx.client_contact:
        parts.append(
            f"**Client contact (recipient):** {ctx.client_contact.name} "
            f"<{ctx.client_contact.email}> ({ctx.client_contact.company})"
        )
    if ctx.candidate:
        parts.append(f"**Candidate:** {ctx.candidate.name}")
    if ctx.loop:
        parts.append(f"**Loop:** {ctx.loop.title}")
    return "\n".join(parts) if parts else "(No additional recipient info)"


def build_draft_prompt(ctx: AgentContext, classification: ClassificationResult) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the draft generation step.

    Fetches prompt templates from Langfuse (with fallback to hardcoded).
    """
    system = _get_langfuse_prompt(_DRAFTER_SYSTEM_NAME, _DRAFT_SYSTEM_FALLBACK)

    user_template = _get_langfuse_prompt(_DRAFTER_USER_NAME, _DRAFT_USER_FALLBACK)

    in_reply_to = ctx.new_message.message_id_header or "null"

    user = _compile_template(
        user_template,
        classification=classification.classification.value,
        action=classification.suggested_action.value,
        reasoning=classification.reasoning,
        coordinator_name=ctx.coordinator.name,
        coordinator_email=ctx.coordinator.email,
        recipient_info=_format_recipient_info(classification, ctx),
        thread_history=_format_thread_history(ctx),
        task_description=_describe_task(classification, ctx),
        in_reply_to=in_reply_to,
    )

    return system, user
