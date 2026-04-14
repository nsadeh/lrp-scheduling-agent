"""Classification output models.

These models define the contract between the LLM classifier and all
downstream systems (suggestion persistence, sidebar UI, analytics).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from api.scheduling.models import StageState  # noqa: TC001


class EmailClassification(StrEnum):
    NEW_INTERVIEW_REQUEST = "new_interview_request"
    AVAILABILITY_RESPONSE = "availability_response"
    TIME_CONFIRMATION = "time_confirmation"
    RESCHEDULE_REQUEST = "reschedule_request"
    CANCELLATION = "cancellation"
    FOLLOW_UP_NEEDED = "follow_up_needed"
    INFORMATIONAL = "informational"
    NOT_SCHEDULING = "not_scheduling"


class SuggestedAction(StrEnum):
    ADVANCE_STAGE = "advance_stage"
    CREATE_LOOP = "create_loop"
    LINK_THREAD = "link_thread"
    DRAFT_EMAIL = "draft_email"
    MARK_COLD = "mark_cold"
    ASK_COORDINATOR = "ask_coordinator"
    NO_ACTION = "no_action"


class SuggestionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    AUTO_APPLIED = "auto_applied"
    SUPERSEDED = "superseded"


class SuggestionItem(BaseModel):
    """A single suggestion from the LLM classifier."""

    classification: EmailClassification
    action: SuggestedAction
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    target_state: StageState | None = None
    target_loop_id: str | None = None
    target_stage_id: str | None = None
    auto_advance: bool = False
    extracted_entities: dict[str, Any] = {}
    questions: list[str] = []


class ClassificationResult(BaseModel):
    """LLM output schema — one or more suggestions per email."""

    suggestions: list[SuggestionItem]
    reasoning: str


class Suggestion(BaseModel):
    """A persisted suggestion row from agent_suggestions."""

    id: str
    coordinator_email: str
    gmail_message_id: str
    gmail_thread_id: str
    loop_id: str | None = None
    stage_id: str | None = None
    classification: EmailClassification
    action: SuggestedAction
    auto_advance: bool = False
    confidence: float
    summary: str
    target_state: StageState | None = None
    extracted_entities: dict[str, Any] = {}
    questions: list[str] = []
    reasoning: str | None = None
    status: SuggestionStatus = SuggestionStatus.PENDING
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    created_at: datetime | None = None
