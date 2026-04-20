"""Build forwarded email bodies — Gmail-style quoted thread history.

When the scheduling agent forwards an email to a recipient who is new to the
thread (e.g. a recruiter), Gmail's per-participant thread view won't give that
recipient any context: they see only the new message. We fix this by building a
plain-text quoted history body server-side at send time, matching Gmail's native
"Forwarded message" block format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.gmail.models import EmailAddress, Message, Thread

_FORWARD_SEPARATOR = "---------- Forwarded message ----------"


def _format_address(addr: EmailAddress) -> str:
    if addr.name:
        return f"{addr.name} <{addr.email}>"
    return addr.email


def _format_addresses(addrs: list[EmailAddress]) -> str:
    return ", ".join(_format_address(a) for a in addrs)


def _format_message_block(msg: Message) -> str:
    # "%-d" / "%-I" drop leading zeros (GNU/BSD strftime — works on macOS + Linux).
    date_str = msg.date.strftime("%a, %b %-d, %Y at %-I:%M %p")
    lines = [
        _FORWARD_SEPARATOR,
        f"From: {_format_address(msg.from_)}",
        f"Date: {date_str}",
        f"Subject: {msg.subject}",
        f"To: {_format_addresses(msg.to)}",
    ]
    if msg.cc:
        lines.append(f"Cc: {_format_addresses(msg.cc)}")
    lines.append("")
    lines.append(msg.body_text.rstrip())
    return "\n".join(lines)


def build_forwarded_body(note: str, thread: Thread) -> str:
    """Append Gmail-style quoted history of `thread` below `note`.

    Messages are emitted in chronological order (oldest first), matching the
    order already provided by `Thread.messages`. Each message is wrapped in a
    "---------- Forwarded message ----------" header block.
    """
    note_trimmed = note.rstrip()
    if not thread.messages:
        return note_trimmed
    blocks = [_format_message_block(m) for m in thread.messages]
    return "\n\n".join([note_trimmed, *blocks])


def prefix_forward_subject(subject: str) -> str:
    """Prefix ``Fwd:`` unless the subject already begins with one (case-insensitive)."""
    if subject.lstrip().lower().startswith("fwd:"):
        return subject
    return f"Fwd: {subject}"
