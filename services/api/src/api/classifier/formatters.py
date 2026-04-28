"""Prompt context formatters — render domain objects into human-readable text blocks.

Each format_* function converts a domain model into a string that becomes a
template variable in the LangFuse prompt. The prompts never reference model
fields directly — this layer is the decoupling point.

Token budget: thread history is truncated from oldest messages to stay within
a configurable character limit (~4 chars/token as a rough proxy).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.gmail.models import Message
    from api.scheduling.models import Loop, LoopEvent

# Default thread history budget: ~3000 tokens x 4 chars/token = 12000 chars
DEFAULT_THREAD_CHAR_BUDGET = 12_000


def format_email(message: Message, direction: str, message_type: str = "") -> str:
    """Format a single email into a human-readable block for the LLM."""
    from_str = (
        f"{message.from_.name} <{message.from_.email}>"
        if message.from_.name
        else message.from_.email
    )
    to_str = ", ".join(f"{a.name} <{a.email}>" if a.name else a.email for a in message.to)
    cc_str = ", ".join(f"{a.name} <{a.email}>" if a.name else a.email for a in message.cc)

    lines = [
        f"From: {from_str}",
        f"To: {to_str}",
    ]
    if cc_str:
        lines.append(f"CC: {cc_str}")
    lines.append(f"Subject: {message.subject}")
    lines.append(f"Date: {message.date.isoformat()}")
    lines.append(f"Direction: {direction}")
    if message_type:
        lines.append(f"Message-Type: {message_type}")
    lines.extend(["", message.body_text.strip()])
    return "\n".join(lines)


def format_thread_history(
    messages: list[Message],
    current_message_id: str,
    char_budget: int = DEFAULT_THREAD_CHAR_BUDGET,
) -> str:
    """Format thread history for context, newest first, truncated from oldest.

    Excludes the current message (which is provided separately as {{email}}).
    """
    prior = [m for m in messages if m.id != current_message_id]
    # Sort newest first
    prior.sort(key=lambda m: m.date, reverse=True)

    if not prior:
        return "No prior messages in this thread."

    formatted_parts: list[str] = []
    total_chars = 0
    truncated_count = 0

    for msg in prior:
        from_str = f"{msg.from_.name} <{msg.from_.email}>" if msg.from_.name else msg.from_.email
        block = (
            f"--- Message ({msg.date.strftime('%Y-%m-%d %H:%M')}) ---\n"
            f"From: {from_str}\n"
            f"Subject: {msg.subject}\n\n"
            f"{msg.body_text.strip()}\n"
        )

        if total_chars + len(block) > char_budget and formatted_parts:
            truncated_count = len(prior) - len(formatted_parts)
            break

        formatted_parts.append(block)
        total_chars += len(block)

    result = "\n".join(formatted_parts)
    if truncated_count > 0:
        result += f"\n[...{truncated_count} earlier message(s) truncated...]"

    return result


def format_loop_state(loop: Loop | None) -> str:
    """Format a loop's current state for the LLM — stages, actors, key info."""
    if loop is None:
        return "No matching loop found for this thread."

    lines = [
        f"Loop: {loop.title} (ID: {loop.id})",
    ]

    if loop.candidate:
        lines.append(f"Candidate: {loop.candidate.name}")
    if loop.client_contact:
        company = loop.client_contact.company or "Unknown"
        lines.append(f"Client: {loop.client_contact.name} ({company})")
    if loop.recruiter:
        lines.append(f"Recruiter: {loop.recruiter.name} <{loop.recruiter.email}>")

    if loop.stages:
        lines.append("\nStages:")
        for stage in loop.stages:
            lines.append(f"  - {stage.name} (ID: {stage.id}): {stage.state.value}")
    else:
        lines.append("\nNo stages yet.")

    return "\n".join(lines)


def format_linked_loops(loops: list[Loop]) -> str:
    """Format ALL loops linked to the current thread.

    Multi-loop threads (one Gmail thread linked to two or more loops, e.g.
    two candidates discussed in the same chain) require the LLM to pick
    which loop a suggestion targets via `target_loop_id`. This function
    renders every linked loop so the LLM can disambiguate.
    """
    if not loops:
        return "No matching loop found for this thread."

    if len(loops) == 1:
        return format_loop_state(loops[0])

    blocks = [
        f"This thread is linked to {len(loops)} loops. "
        "When emitting a loop-scoped suggestion (DRAFT_EMAIL, ADVANCE_STAGE, "
        "MARK_COLD), set `target_loop_id` to the specific loop you mean.",
        "",
    ]
    for loop in loops:
        blocks.append(format_loop_state(loop))
        blocks.append("")
    return "\n".join(blocks).rstrip()


def format_active_loops(loops: list[Loop]) -> str:
    """Format coordinator's active loops summary for thread-to-loop matching."""
    if not loops:
        return "No active loops for this coordinator."

    lines = ["Active scheduling loops:"]
    for loop in loops:
        candidate_name = loop.candidate.name if loop.candidate else "Unknown"
        client_company = (
            loop.client_contact.company
            if loop.client_contact and loop.client_contact.company
            else "Unknown"
        )
        stage_summary = ", ".join(f"{s.name}={s.state.value}" for s in loop.stages if s.is_active)
        lines.append(
            f"  - {loop.title} (ID: {loop.id}): "
            f"Candidate={candidate_name}, Client={client_company}"
            f"{f', Stages: [{stage_summary}]' if stage_summary else ''}"
        )

    return "\n".join(lines)


def format_events(events: list[LoopEvent], limit: int = 10) -> str:
    """Format recent loop events for context."""
    if not events:
        return "No events recorded for this loop."

    recent = events[-limit:]
    lines = ["Recent events:"]
    for evt in recent:
        lines.append(
            f"  - [{evt.occurred_at.strftime('%Y-%m-%d %H:%M')}] "
            f"{evt.event_type.value} by {evt.actor_email}"
        )

    if len(events) > limit:
        lines.append(f"  [...{len(events) - limit} earlier events omitted...]")

    return "\n".join(lines)


def format_stage_states() -> str:
    """Format all stage states with descriptions for the system prompt."""
    from api.scheduling.models import NEXT_ACTIONS, StageState

    lines = ["Stage states:"]
    for state in StageState:
        lines.append(f"  - {state.value}: {NEXT_ACTIONS[state]}")
    return "\n".join(lines)


def format_transitions() -> str:
    """Format allowed transitions for the system prompt."""
    from api.scheduling.models import ALLOWED_TRANSITIONS

    lines = ["Allowed state transitions:"]
    for from_state, to_states in ALLOWED_TRANSITIONS.items():
        if to_states:
            targets = ", ".join(sorted(s.value for s in to_states))
            lines.append(f"  {from_state.value} → {targets}")
        else:
            lines.append(f"  {from_state.value} → (terminal, no transitions)")
    return "\n".join(lines)
