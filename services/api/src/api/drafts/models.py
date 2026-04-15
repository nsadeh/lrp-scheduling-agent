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
    status: DraftStatus = DraftStatus.GENERATED
    sent_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# LLM I/O models
# ---------------------------------------------------------------------------


class GenerateDraftInput(BaseModel):
    """Template variables for the draft-email-v1 LangFuse prompt."""

    classification: str
    recipient_type: str  # "client" or "recruiter"
    recipient_name: str
    candidate_name: str
    coordinator_name: str
    stage_state: str
    extracted_entities: str  # JSON string of availability, phone numbers, etc.
    thread_summary: str  # Recent thread context for reply coherence
    loop_context: str  # Loop title, participants, stage info


class DraftOutput(BaseModel):
    """LLM output — the generated email body."""

    body: str
    reasoning: str  # Why this content was chosen (for debugging/eval, not shown to user)
