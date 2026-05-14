"""Prompt context formatters — render domain objects into human-readable text blocks.

Each format_* function converts a domain model into a string that becomes a
template variable in the LangFuse prompt. The prompts never reference model
fields directly — this layer is the decoupling point.

Token budget: thread history is truncated from oldest messages to stay within
a configurable character limit (~4 chars/token as a rough proxy).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as _xml_escape
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from api.classifier.models import Suggestion
    from api.gmail.models import Message
    from api.scheduling.models import Loop, LoopEvent

# Default thread history budget: ~3000 tokens x 4 chars/token = 12000 chars
DEFAULT_THREAD_CHAR_BUDGET = 12_000

# LRP runs out of New York; "ET" in the LLM date string follows local
# coordinator time so day-of-week is unambiguous around midnight UTC.
_LLM_TZ = ZoneInfo("America/New_York")


def format_llm_datetime(dt: datetime | None = None) -> str:
    """Render a datetime as ``"Wednesday, May 13 2026, 8:02 PM ET"``.

    Includes the day-of-week explicitly because passing a bare ISO date
    caused the LLM to hallucinate weekdays (e.g. calling Wednesday "Friday"
    in scheduling drafts). ``dt`` defaults to ``datetime.now(UTC)``;
    naive datetimes are interpreted as UTC.
    """
    if dt is None:
        dt = datetime.now(UTC)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(_LLM_TZ)
    # Hour without leading zero (strftime("%I") gives "08"; we want "8").
    hour_12 = local.hour % 12 or 12
    return local.strftime(f"%A, %B {local.day} {local.year}, {hour_12}:%M %p ET")


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
    """Format a loop's current state for the LLM — actors and current state."""
    if loop is None:
        return "No matching loop found for this thread."

    lines = [
        f"Loop: {loop.title} (ID: {loop.id})",
        f"State: {loop.state.value}",
    ]

    if loop.candidate:
        lines.append(f"Candidate: {loop.candidate.name}")
    if loop.client_contact:
        company = loop.client_contact.company or "Unknown"
        lines.append(f"Client: {loop.client_contact.name} ({company})")
    if loop.recruiter:
        lines.append(f"Recruiter: {loop.recruiter.name} <{loop.recruiter.email}>")

    return "\n".join(lines)


def format_linked_loops(loops: list[Loop]) -> str:
    """Format every loop linked to the current thread.

    Multi-loop threads (one Gmail thread linked to two or more loops)
    render as multiple blocks separated by a blank line. The LangFuse
    classifier prompt — not this formatter — owns the instructions about
    how the LLM should disambiguate via `target_loop_id`.
    """
    if not loops:
        return "No matching loop found for this thread."

    if len(loops) == 1:
        return format_loop_state(loops[0])

    blocks: list[str] = []
    for loop in loops:
        blocks.append(format_loop_state(loop))
        blocks.append("")
    return "\n".join(blocks).rstrip()


DEFAULT_ACTIVE_LOOPS_CHAR_BUDGET = 20_000


def format_active_loops(
    loops: list[Loop],
    char_budget: int = DEFAULT_ACTIVE_LOOPS_CHAR_BUDGET,
) -> str:
    """Format coordinator's active loops summary for thread-to-loop matching.

    Truncates from the end when the formatted output exceeds char_budget,
    following the same pattern as format_thread_history.
    """
    if not loops:
        return "No active loops for this coordinator."

    header = f"Active scheduling loops ({len(loops)} total):"
    lines = [header]
    total_chars = len(header)
    truncated_count = 0

    for i, loop in enumerate(loops):
        candidate_name = loop.candidate.name if loop.candidate else "Unknown"
        client_company = (
            loop.client_contact.company
            if loop.client_contact and loop.client_contact.company
            else "Unknown"
        )
        line = (
            f"  - {loop.title} (ID: {loop.id}): "
            f"Candidate={candidate_name}, Client={client_company}, "
            f"State={loop.state.value}"
        )

        if total_chars + len(line) + 1 > char_budget and i > 0:
            truncated_count = len(loops) - i
            break

        lines.append(line)
        total_chars += len(line) + 1  # +1 for newline

    if truncated_count > 0:
        lines.append(f"  [...{truncated_count} more loop(s) truncated...]")

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


def format_pending_suggestions(suggestions: list[Suggestion]) -> str:
    """Format pending suggestions so the LLM can see what's already queued."""
    if not suggestions:
        return "No current pending suggestions."

    lines = ["Pending suggestions awaiting coordinator review:"]
    for sug in suggestions:
        line = f"  - [{sug.loop_id}] {sug.action}"
        if sug.summary:
            line += f": {sug.summary}"
        if sug.action_data:
            highlights = _action_data_highlights(sug.action, sug.action_data)
            if highlights:
                line += f" ({highlights})"
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# XML formatters — feed the next-action-agent prompt's structured inputs
# ---------------------------------------------------------------------------


def _escape(value: str | None) -> str:
    """XML-escape a possibly-null string."""
    return _xml_escape(value or "")


def _format_address(name: str | None, email: str) -> str:
    """Render an address as 'Name email' (matching the prompt's expected shape)."""
    if name:
        return f"{name} {email}"
    return email


_DIRECTION_LABELS = {
    "incoming": "inbound",
    "outgoing": "outbound",
}


def format_email_xml(message: Message, direction: str) -> str:
    """Render a single message as an <email> XML block.

    Direction is normalised to ``inbound`` / ``outbound`` to match the prompt's
    vocabulary (the internal enum uses ``incoming`` / ``outgoing``).
    """
    direction_label = _DIRECTION_LABELS.get(direction, direction)
    from_str = _format_address(message.from_.name, message.from_.email)
    to_str = ", ".join(_format_address(a.name, a.email) for a in message.to)
    cc_str = ", ".join(_format_address(a.name, a.email) for a in message.cc)

    lines = [
        f"<email direction='{_escape(direction_label)}'>",
        f"  <timestamp>{_escape(message.date.isoformat())}</timestamp>",
        f"  <from>{_escape(from_str)}</from>",
        f"  <to>{_escape(to_str)}</to>",
    ]
    if cc_str:
        lines.append(f"  <cc>{_escape(cc_str)}</cc>")
    lines.append(f"  <subject>{_escape(message.subject)}</subject>")
    lines.append(f"  <body>{_escape(message.body_text.strip())}</body>")
    lines.append("</email>")
    return "\n".join(lines)


def format_thread_history_xml(
    messages: list[Message],
    current_message_id: str,
    coordinator_email: str,
    char_budget: int = DEFAULT_THREAD_CHAR_BUDGET,
) -> str:
    """Render thread history as concatenated <email> blocks, oldest first.

    Excludes the trigger message (provided separately as ``{{email}}``).
    Direction is per-message: outbound when sent by the coordinator's mailbox,
    inbound otherwise. Truncates from the oldest end to stay within the
    char budget; emits an XML comment marker when truncation occurs.
    """
    prior = [m for m in messages if m.id != current_message_id]
    prior.sort(key=lambda m: m.date)  # oldest first

    if not prior:
        return "<!-- No prior messages in this thread -->"

    # Render newest first so we can keep the most recent N within budget,
    # then reverse to emit oldest-first.
    rendered_newest_first: list[str] = []
    total_chars = 0
    truncated_count = 0

    for msg in reversed(prior):
        direction = "outbound" if msg.from_.email == coordinator_email else "inbound"
        block = format_email_xml(msg, direction)
        if total_chars + len(block) > char_budget and rendered_newest_first:
            truncated_count = len(prior) - len(rendered_newest_first)
            break
        rendered_newest_first.append(block)
        total_chars += len(block) + 1  # newline between blocks

    parts: list[str] = []
    if truncated_count > 0:
        parts.append(f"<!-- {truncated_count} earlier message(s) truncated -->")
    parts.extend(reversed(rendered_newest_first))
    return "\n".join(parts)


def _format_actor(prefix_tag: str, value: str | None) -> str:
    """Render a single actor tag, defaulting to 'Unknown' when null."""
    return f"<{prefix_tag}>{_escape(value or 'Unknown')}</{prefix_tag}>"


def _format_client_contact(loop: Loop) -> str:
    cc = loop.client_contact
    if cc is None:
        return "<client-contact>Unknown</client-contact>"
    name = cc.name or cc.email
    company = cc.company or ""
    label = f"{name}, {company}".rstrip(", ").strip()
    return f"<client-contact>{_escape(label or 'Unknown')}</client-contact>"


def _format_pending_suggestion_body(sug: Suggestion) -> str:
    """Render a pending suggestion's action_data so the agent can recognize
    duplicates and target the right one for expire_suggestion.

    Only draft_email and ask_coordinator end up pending in practice — the other
    action types auto-resolve via the resolver registry. We format those two
    explicitly and fall back to the summary for anything else.
    """
    summary = sug.summary or ""
    data = sug.action_data or {}

    if sug.action == "draft_email":
        recipient = data.get("recipient_type", "")
        body = (data.get("body") or "").strip()
        return (
            f"      <summary>{_escape(summary)}</summary>\n"
            f"      <recipient-type>{_escape(recipient)}</recipient-type>\n"
            f"      <body>{_escape(body)}</body>"
        )
    if sug.action == "ask_coordinator":
        question = (data.get("question") or "").strip()
        return (
            f"      <summary>{_escape(summary)}</summary>\n"
            f"      <question>{_escape(question)}</question>"
        )
    if sug.action == "update_actor":
        role = (data.get("role") or "").strip()
        return f"      <summary>{_escape(summary)}</summary>\n      <role>{_escape(role)}</role>"
    # Fallback for any future pending action types — keep the body terse.
    return f"      <summary>{_escape(summary)}</summary>"


def _format_pending_suggestions_xml(pending: list[Suggestion]) -> str:
    if not pending:
        return "<pending-suggestions>No current suggestions</pending-suggestions>"
    children: list[str] = []
    for sug in pending:
        body = _format_pending_suggestion_body(sug)
        children.append(
            f"    <suggestion id='{_escape(sug.id)}' action='{_escape(sug.action)}'>\n"
            f"{body}\n"
            f"    </suggestion>"
        )
    inner = "\n".join(children)
    return f"<pending-suggestions>\n{inner}\n  </pending-suggestions>"


def format_loop_xml(loop: Loop, pending_for_loop: list[Suggestion]) -> str:
    """Render a single loop as a <loop> XML block."""
    coordinator_name = loop.coordinator.name if loop.coordinator else None
    recruiter_name = loop.recruiter.name if loop.recruiter else None
    candidate_name = loop.candidate.name if loop.candidate else None
    cm_name = loop.client_manager.name if loop.client_manager else None

    pending_block = _format_pending_suggestions_xml(pending_for_loop)
    # Indent the pending block by two spaces so it aligns with siblings.
    pending_block_indented = "\n".join("  " + line for line in pending_block.split("\n"))

    lines = [
        f"<loop id='{_escape(loop.id)}'>",
        f"  <stage>{_escape(loop.state.value)}</stage>",
        "  <actors>",
        f"    {_format_actor('coordinator', coordinator_name)}",
        f"    {_format_client_contact(loop)}",
        f"    {_format_actor('client-manager', cm_name)}",
        f"    {_format_actor('recruiter', recruiter_name)}",
        f"    {_format_actor('candidate', candidate_name)}",
        "  </actors>",
        pending_block_indented,
        "</loop>",
    ]
    return "\n".join(lines)


def format_loops_xml(
    loops: list[Loop],
    pending_by_loop: dict[str, list[Suggestion]],
) -> str:
    """Render all loops linked to the thread as concatenated <loop> blocks."""
    if not loops:
        return "<!-- No loops linked to this thread -->"
    return "\n".join(format_loop_xml(lp, pending_by_loop.get(lp.id, [])) for lp in loops)


def _action_data_highlights(action: str, action_data: dict) -> str:
    if action == "advance_stage":
        return f"target_stage={action_data.get('target_stage', '?')}"
    elif action == "draft_email":
        recipient = action_data.get("recipient_type", "?")
        body = action_data.get("body", "")
        body_preview = body[:80] + ("..." if len(body) > 80 else "")
        return f'to={recipient}, body="{body_preview}"'
    elif action == "ask_coordinator":
        q = action_data.get("question", "")
        preview = q[:80] + ("..." if len(q) > 80 else "")
        return f'question="{preview}"'
    return ""
