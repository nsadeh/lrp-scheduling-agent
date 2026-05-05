"""Denormalized models for the suggestion-centric overview UI.

These models combine data from agent_suggestions, loops, candidates,
client_contacts, and email_drafts into flat views for card rendering.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003

from pydantic import BaseModel

from api.classifier.models import Suggestion  # noqa: TC001
from api.drafts.models import EmailDraft  # noqa: TC001


class SuggestionView(BaseModel):
    """Denormalized suggestion for UI rendering — one row per pending suggestion."""

    suggestion: Suggestion
    loop_title: str | None = None
    loop_state: str | None = None
    candidate_name: str | None = None
    client_company: str | None = None
    draft: EmailDraft | None = None
    # Known actor emails on the loop — surfaced as small-print hints under
    # JIT inputs so coordinators can see what we already have when asking
    # for a missing one.
    client_contact_name: str | None = None
    client_contact_email: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    client_manager_name: str | None = None
    client_manager_email: str | None = None


class LoopSuggestionGroup(BaseModel):
    """A group of suggestions sharing the same loop, for rendering as one Section."""

    loop_id: str | None = None
    loop_title: str | None = None
    candidate_name: str | None = None
    client_company: str | None = None
    suggestions: list[SuggestionView]
    oldest_created_at: datetime
