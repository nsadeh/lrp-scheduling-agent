"""Domain models for scheduling loops."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class StageState(StrEnum):
    NEW = "new"
    AWAITING_CANDIDATE = "awaiting_candidate"
    AWAITING_CLIENT = "awaiting_client"
    SCHEDULED = "scheduled"
    COMPLETE = "complete"
    COLD = "cold"


# What the coordinator needs to do next for each state
NEXT_ACTIONS: dict[StageState, str] = {
    StageState.NEW: "Email recruiter for availability",
    StageState.AWAITING_CANDIDATE: "Waiting on candidate availability",
    StageState.AWAITING_CLIENT: "Waiting on client to pick times",
    StageState.SCHEDULED: "Interview scheduled",
    StageState.COMPLETE: "Complete",
    StageState.COLD: "Stalled",
}

# Priority for sorting (lower = more urgent, needs coordinator action)
STATE_PRIORITY: dict[StageState, int] = {
    StageState.NEW: 0,
    StageState.AWAITING_CANDIDATE: 2,
    StageState.AWAITING_CLIENT: 2,
    StageState.SCHEDULED: 3,
    StageState.COMPLETE: 4,
    StageState.COLD: 5,
}


class EventType(StrEnum):
    # Loop-level state events
    STATE_ADVANCED = "state_advanced"
    LOOP_MARKED_COLD = "loop_marked_cold"
    LOOP_REVIVED = "loop_revived"
    EMAIL_DRAFTED = "email_drafted"
    EMAIL_SENT = "email_sent"
    LOOP_CREATED = "loop_created"
    THREAD_LINKED = "thread_linked"
    THREAD_UNLINKED = "thread_unlinked"
    ACTOR_UPDATED = "actor_updated"
    NOTE_ADDED = "note_added"


class Coordinator(BaseModel):
    id: str
    name: str
    email: str
    created_at: datetime


class Contact(BaseModel):
    id: str
    name: str
    email: str
    role: str
    company: str | None = None
    photo_url: str | None = None
    created_at: datetime


class ClientContact(BaseModel):
    id: str
    name: str
    email: str
    company: str | None = None
    created_at: datetime


class Candidate(BaseModel):
    id: str
    name: str
    notes: str | None = None
    created_at: datetime


class LoopEvent(BaseModel):
    id: str
    loop_id: str
    event_type: EventType
    data: dict[str, Any]
    actor_email: str
    occurred_at: datetime


class EmailThread(BaseModel):
    id: str
    loop_id: str
    gmail_thread_id: str
    subject: str | None = None
    linked_at: datetime


class Loop(BaseModel):
    id: str
    coordinator_id: str
    client_contact_id: str | None = None
    recruiter_id: str | None = None
    client_manager_id: str | None = None
    candidate_id: str
    title: str
    state: StageState = StageState.NEW
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    # Nested relations (populated by service)
    coordinator: Coordinator | None = None
    client_contact: ClientContact | None = None
    recruiter: Contact | None = None
    client_manager: Contact | None = None
    candidate: Candidate | None = None
    email_threads: list[EmailThread] = []

    @property
    def is_active(self) -> bool:
        return self.state not in (StageState.COMPLETE, StageState.COLD)

    @property
    def is_actionable(self) -> bool:
        """Coordinator needs to do something (not just waiting)."""
        return self.state == StageState.NEW

    @property
    def next_action(self) -> str:
        return NEXT_ACTIONS[self.state]


class LoopSummary(BaseModel):
    """Lightweight loop info for the status board."""

    loop_id: str
    title: str
    candidate_name: str
    client_company: str
    state: StageState = StageState.NEW
    next_action: str | None = None


class StatusBoard(BaseModel):
    """Grouped loops for the homepage status board."""

    action_needed: list[LoopSummary] = []
    waiting: list[LoopSummary] = []
    scheduled: list[LoopSummary] = []
    complete: list[LoopSummary] = []
    cold: list[LoopSummary] = []
