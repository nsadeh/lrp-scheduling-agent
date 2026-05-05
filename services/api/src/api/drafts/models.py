"""Models for email drafts.

EmailDraft (email_drafts row) and DraftStatus enum.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for Pydantic
from enum import StrEnum
from typing import Any

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
    coordinator_email: str
    to_emails: list[str]
    cc_emails: list[str] = []
    subject: str
    body: str = ""
    gmail_thread_id: str | None = None
    is_forward: bool = False
    status: DraftStatus = DraftStatus.GENERATED
    # Coordinator's in-flight contact picks for the JIT widget (recruiter /
    # client / CM). Keyed by role; values are {"name": str, "email": str,
    # "company"?: str}. Cleared at send time after the contacts are
    # committed to the loop.
    pending_jit_data: dict[str, Any] = {}
    sent_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
