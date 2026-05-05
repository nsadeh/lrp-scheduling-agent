"""Classification models — LLM output schema and database models.

Two model families:
- ClassificationResult / SuggestionItem: LLM output schema (what the endpoint parses)
- Suggestion: database model (what gets persisted to agent_suggestions)

action_data is a polymorphic JSONB field. Per-action shape is defined by the
typed models below (AdvanceStageData, DraftEmailData, etc.) and dispatched by
ACTION_DATA_MODELS. The LLM emits a loose dict; guardrails parse it into the
correct typed shape and feed validation errors back through the retry loop.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum
from typing import Any, Literal

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


class AdvanceStageData(BaseModel):
    """Action data for ADVANCE_STAGE — the new state the loop should move to."""

    target_stage: StageState


class DraftEmailData(BaseModel):
    """Action data for DRAFT_EMAIL — instructions to the drafter agent.

    The drafter only knows tone rules; the directive must encode all the
    context it needs (candidate availability, client preferences, zoom links,
    etc.) since action_data no longer carries a separate extracted_entities
    blob.
    """

    directive: str
    recipient_type: Literal["client", "recruiter", "internal"]


class AskCoordinatorData(BaseModel):
    """Action data for ASK_COORDINATOR — a single question to surface in the UI."""

    question: str


class NoActionData(BaseModel):
    """Action data for NO_ACTION — empty by design."""


class LinkThreadData(BaseModel):
    """Action data for LINK_THREAD — empty; target_loop_id on the suggestion is authoritative."""


class CreateLoopExtraction(BaseModel):
    """Typed payload for CREATE_LOOP action_data.

    Emitted by the classifier's CREATE_LOOP suggestions AND by the on-demand
    manual-path extractor (``extract_create_loop_fields``). Every field is
    optional — the consumer (the create-loop form) tolerates any subset and
    falls back to deterministic prefill / stored contact rows when a field
    is null.
    """

    candidate_name: str | None = None
    client_name: str | None = None
    client_email: str | None = None
    client_company: str | None = None
    cm_name: str | None = None
    cm_email: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None


# Dispatcher: action -> ActionData class. Used by guardrails to validate
# the loose dict that the LLM emits matches the expected per-action schema.
ACTION_DATA_MODELS: dict[SuggestedAction, type[BaseModel]] = {
    SuggestedAction.ADVANCE_STAGE: AdvanceStageData,
    SuggestedAction.DRAFT_EMAIL: DraftEmailData,
    SuggestedAction.ASK_COORDINATOR: AskCoordinatorData,
    SuggestedAction.NO_ACTION: NoActionData,
    SuggestedAction.LINK_THREAD: LinkThreadData,
    SuggestedAction.CREATE_LOOP: CreateLoopExtraction,
}


# -- LLM output schema --


class SuggestionItem(BaseModel):
    """Single suggestion from the LLM — part of ClassificationResult."""

    classification: EmailClassification
    action: SuggestedAction
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    reasoning: str
    target_loop_id: str | None = None
    action_data: dict[str, Any] = {}


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
    classification: EmailClassification
    action: SuggestedAction
    confidence: float
    summary: str
    action_data: dict[str, Any] = {}
    reasoning: str | None = None
    status: SuggestionStatus = SuggestionStatus.PENDING
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    created_at: datetime | None = None
