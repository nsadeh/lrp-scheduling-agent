"""Prompt builder functions for the email classifier.

Each function renders a domain object into a human-readable text block
that gets injected into LangFuse prompt templates as a template variable.
The LangFuse template only knows about these pre-formatted strings —
it never references message.from_.email or loop.stages[0].state directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.scheduling.models import (
    ALLOWED_TRANSITIONS,
    NEXT_ACTIONS,
    StageState,
)

if TYPE_CHECKING:
    from api.gmail.models import Message
    from api.scheduling.models import Loop, LoopEvent

# Maximum characters for thread history before truncating
THREAD_HISTORY_CHAR_BUDGET = 12_000


def format_email(message: Message) -> str:
    """Format a single email message into a readable block."""
    if message.from_.name:
        from_ = f"{message.from_.name} <{message.from_.email}>"
    else:
        from_ = message.from_.email
    to = ", ".join(f"{a.name} <{a.email}>" if a.name else a.email for a in message.to)
    cc = ", ".join(f"{a.name} <{a.email}>" if a.name else a.email for a in message.cc)

    lines = [
        f"From: {from_}",
        f"To: {to}",
    ]
    if cc:
        lines.append(f"CC: {cc}")
    lines.extend(
        [
            f"Subject: {message.subject}",
            f"Date: {message.date.isoformat()}",
            "",
            message.body_text.strip() if message.body_text else "(empty body)",
        ]
    )
    return "\n".join(lines)


def format_thread_history(messages: list[Message], *, exclude_id: str | None = None) -> str:
    """Format thread messages newest-first, truncating oldest if over budget.

    Args:
        messages: All messages in the thread, any order.
        exclude_id: Message ID to exclude (the current message being classified).
    """
    # Sort newest first
    sorted_msgs = sorted(
        [m for m in messages if m.id != exclude_id],
        key=lambda m: m.date,
        reverse=True,
    )

    if not sorted_msgs:
        return "(no prior messages in thread)"

    parts = []
    total_chars = 0

    for i, msg in enumerate(sorted_msgs):
        formatted = format_email(msg)
        total_chars += len(formatted)

        if total_chars > THREAD_HISTORY_CHAR_BUDGET:
            remaining = len(sorted_msgs) - i
            parts.append(f"[...{remaining} earlier message(s) truncated...]")
            break

        parts.append(f"--- Message {i + 1} ---\n{formatted}")

    return "\n\n".join(parts)


def format_loop_state(loop: Loop | None) -> str:
    """Format a loop's current state for the classifier."""
    if loop is None:
        return "No matching loop found for this thread."

    lines = [
        f"Loop: {loop.title} (ID: {loop.id})",
    ]

    if loop.candidate:
        lines.append(f"Candidate: {loop.candidate.name}")
    if loop.client_contact:
        lines.append(f"Client: {loop.client_contact.name} ({loop.client_contact.company})")
    if loop.recruiter:
        lines.append(f"Recruiter: {loop.recruiter.name} <{loop.recruiter.email}>")
    if loop.client_manager:
        lines.append(f"Client Manager: {loop.client_manager.name}")

    lines.append("")
    lines.append("Stages:")
    for stage in loop.stages:
        if stage.is_active:
            status_marker = "→"
        elif stage.state == StageState.COMPLETE:
            status_marker = "✓"
        else:
            status_marker = "—"
        allowed = ALLOWED_TRANSITIONS.get(stage.state, set())
        transitions = ", ".join(sorted(s.value for s in allowed)) if allowed else "none"
        lines.append(
            f"  {status_marker} {stage.name}: {stage.state.value} "
            f"(allowed transitions: {transitions})"
        )

    return "\n".join(lines)


def format_events(events: list[LoopEvent], *, limit: int = 10) -> str:
    """Format recent loop events for context."""
    if not events:
        return "(no events recorded)"

    recent = sorted(events, key=lambda e: e.occurred_at, reverse=True)[:limit]
    lines = []
    for evt in recent:
        ts = evt.occurred_at.strftime("%Y-%m-%d %H:%M")
        lines.append(f"  [{ts}] {evt.event_type.value}: {evt.data}")

    return "\n".join(lines)


def format_active_loops(loops: list[Loop]) -> str:
    """Format coordinator's active loops summary for thread matching."""
    if not loops:
        return "(no active loops)"

    lines = []
    for loop in loops:
        candidate = loop.candidate.name if loop.candidate else "Unknown"
        company = loop.client_contact.company if loop.client_contact else "Unknown"
        urgent = loop.most_urgent_stage
        state = urgent.state.value if urgent else "unknown"
        lines.append(f"  - {loop.title}: {candidate} at {company} [{state}] (ID: {loop.id})")

    return "\n".join(lines)


def format_stage_states() -> str:
    """Format all stage states with descriptions for the system prompt."""
    lines = []
    for state in StageState:
        action = NEXT_ACTIONS.get(state, "")
        lines.append(f"  - {state.value}: {action}")
    return "\n".join(lines)


def format_transitions() -> str:
    """Format allowed transitions for the system prompt."""
    lines = []
    for from_state, to_states in ALLOWED_TRANSITIONS.items():
        if to_states:
            targets = ", ".join(sorted(s.value for s in to_states))
            lines.append(f"  {from_state.value} → {targets}")
        else:
            lines.append(f"  {from_state.value} → (terminal)")
    return "\n".join(lines)


def format_classification_schema() -> str:
    """Format the ClassificationResult JSON schema for the system prompt."""
    from api.classifier.models import ClassificationResult

    return ClassificationResult.model_json_schema(mode="serialization").__repr__()
