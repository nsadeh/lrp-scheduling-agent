"""Classification models — LLM output schema and database models.

Two model families:
- ClassificationResult / SuggestionItem: LLM output schema (what the endpoint parses)
- Suggestion: database model (what gets persisted to agent_suggestions)
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from api.scheduling.models import StageState  # noqa: TC001 — needed at runtime


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


# -- Action data: typed payloads per action type --


class DraftEmailData(BaseModel):
    """Action data for DRAFT_EMAIL suggestions.

    The classifier (agent brain) fills this when it decides an email should
    be drafted. The drafter (tool) reads it as its instruction set.
    """

    # TODO: Define the fields that the classifier should provide
    # to the drafter. Consider:
    #   - What instruction does the drafter need?
    #   - What recipient context matters?
    #   - What entities should be highlighted vs. left in extracted_entities?
    #
    # The drafter prompt only knows tone rules — it relies on this data
    # to know WHAT to write.
    directive: str  # e.g. "Share Claire's availability with the client"
    recipient_type: str  # "client" or "recruiter"


# -- LLM output schema --


class SuggestionItem(BaseModel):
    """Single suggestion from the LLM — part of ClassificationResult."""

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
    action_data: dict[str, Any] = {}  # Typed per action — DraftEmailData for DRAFT_EMAIL


class ClassificationResult(BaseModel):
    """LLM output schema — one or more suggestions per email."""

    suggestions: list[SuggestionItem]
    reasoning: str


# -- Database model --


class Suggestion(BaseModel):
    """Persisted suggestion row from agent_suggestions table."""

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
    action_data: dict[str, Any] = {}
    reasoning: str | None = None
    status: SuggestionStatus = SuggestionStatus.PENDING
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    created_at: datetime | None = None
