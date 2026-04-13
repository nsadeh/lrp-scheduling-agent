"""Email event model, hook protocol, and deterministic message classification.

This module is the integration point between the Gmail push pipeline and
downstream consumers (e.g., the scheduling agent). The pipeline fires an
EmailEvent for every processed message; the consumer implements the
EmailHook protocol to handle it.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from api.gmail.models import EmailAddress, Message  # noqa: TC001 — needed at runtime for Pydantic

logger = logging.getLogger(__name__)


class MessageDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class MessageType(StrEnum):
    NEW_THREAD = "new_thread"
    REPLY = "reply"
    FORWARD = "forward"


class EmailEvent(BaseModel):
    """Structured event fired for every processed email."""

    message: Message
    coordinator_email: str
    direction: MessageDirection
    message_type: MessageType
    new_participants: list[EmailAddress]

    model_config = {"populate_by_name": True}


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


def classify_direction(message: Message, coordinator_email: str) -> MessageDirection:
    """Determine if a message is incoming or outgoing relative to the coordinator."""
    if message.from_.email.lower() == coordinator_email.lower():
        return MessageDirection.OUTGOING
    return MessageDirection.INCOMING


def classify_message_type(
    message: Message,
    prior_messages: list[Message],
) -> tuple[MessageType, list[EmailAddress]]:
    """Classify a message as new thread, reply, or forward.

    A forward is defined as: a message that adds at least one recipient
    not seen in any prior message's from/to/cc fields. This is deterministic
    (no heuristics, no subject-line parsing).
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
    new_participants = [addr for addr in current_recipients if addr.email.lower() not in seen]

    if new_participants:
        return MessageType.FORWARD, new_participants

    return MessageType.REPLY, []
