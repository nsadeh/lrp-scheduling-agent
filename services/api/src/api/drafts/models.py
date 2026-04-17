"""Models for AI-generated email drafts.

Two families:
- LLM I/O: GenerateDraftInput (template vars) and DraftOutput (LLM response)
- Database: EmailDraft (email_drafts row) and DraftStatus enum
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for Pydantic
from enum import StrEnum

from pydantic import BaseModel


class DraftStatus(StrEnum):
    GENERATED = "generated"
    EDITED = "edited"
    SENT = "sent"
    DISCARDED = "discarded"


# ---------------------------------------------------------------------------
# Database model
# ---------------------------------------------------------------------------


class EmailDraft(BaseModel):
    """Persisted row from the email_drafts table."""

    id: str
    suggestion_id: str
    loop_id: str
    stage_id: str
    coordinator_email: str
    to_emails: list[str]
    cc_emails: list[str] = []
    subject: str
    body: str = ""
    gmail_thread_id: str | None = None
    is_forward: bool = False
    status: DraftStatus = DraftStatus.GENERATED
    sent_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# LLM I/O models
# ---------------------------------------------------------------------------


class GenerateDraftInput(BaseModel):
    """Template variables for the draft-email-v1 LangFuse prompt.

    The drafter is a "dumb tool" — it doesn't understand the scheduling state
    machine. The classifier (the agent brain) provides a tight directive of what
    to draft. The drafter just follows tone instructions and the directive.
    """

    draft_directive: str  # From classifier summary, e.g. "Share candidate availability with client"
    recipient_name: str  # First name of the person being emailed
    candidate_name: str
    coordinator_name: str  # For the sign-off
    extracted_entities: str  # JSON string of availability, phone numbers, zoom links, etc.
    thread_messages: str  # Formatted list of recent emails for reply context
    is_external: bool  # True when recipient is outside @longridgepartners.com


class DraftOutput(BaseModel):
    """LLM output — the generated email body."""

    body: str
    reasoning: str  # Why this content was chosen (for debugging/eval, not shown to user)
