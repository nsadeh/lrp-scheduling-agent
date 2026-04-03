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


# Valid transitions: from_state -> set of allowed to_states
ALLOWED_TRANSITIONS: dict[StageState, set[StageState]] = {
    StageState.NEW: {StageState.AWAITING_CANDIDATE, StageState.COLD},
    StageState.AWAITING_CANDIDATE: {StageState.AWAITING_CLIENT, StageState.COLD},
    StageState.AWAITING_CLIENT: {
        StageState.SCHEDULED,
        StageState.AWAITING_CANDIDATE,
        StageState.COLD,
    },
    StageState.SCHEDULED: {StageState.COMPLETE, StageState.COLD},
    StageState.COMPLETE: set(),  # terminal
    StageState.COLD: {
        StageState.NEW,
        StageState.AWAITING_CANDIDATE,
        StageState.AWAITING_CLIENT,
    },  # revival
}

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
    # Stage-level events
    STAGE_CREATED = "stage_created"
    STAGE_ADVANCED = "stage_advanced"
    STAGE_MARKED_COLD = "stage_marked_cold"
    STAGE_REVIVED = "stage_revived"
    EMAIL_DRAFTED = "email_drafted"
    EMAIL_SENT = "email_sent"
    TIME_SLOT_ADDED = "time_slot_added"
    TIME_SLOT_REMOVED = "time_slot_removed"
    # Loop-level events
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
    created_at: datetime


class ClientContact(BaseModel):
    id: str
    name: str
    email: str
    company: str
    created_at: datetime


class Candidate(BaseModel):
    id: str
    name: str
    notes: str | None = None
    created_at: datetime


class TimeSlot(BaseModel):
    id: str
    stage_id: str
    start_time: datetime
    duration_minutes: int
    timezone: str
    zoom_link: str | None = None
    notes: str | None = None
    created_at: datetime


class LoopEvent(BaseModel):
    id: str
    loop_id: str
    stage_id: str | None = None
    event_type: EventType
    data: dict[str, Any]
    actor_email: str
    occurred_at: datetime


class Stage(BaseModel):
    id: str
    loop_id: str
    name: str
    state: StageState
    ordinal: int
    created_at: datetime
    updated_at: datetime
    time_slots: list[TimeSlot] = []

    @property
    def next_action(self) -> str:
        return NEXT_ACTIONS[self.state]

    @property
    def is_active(self) -> bool:
        return self.state not in (StageState.COMPLETE, StageState.COLD)

    @property
    def is_actionable(self) -> bool:
        """Coordinator needs to do something (not just waiting)."""
        return self.state == StageState.NEW


class EmailThread(BaseModel):
    id: str
    loop_id: str
    gmail_thread_id: str
    subject: str | None = None
    linked_at: datetime


class Loop(BaseModel):
    id: str
    coordinator_id: str
    client_contact_id: str
    recruiter_id: str
    client_manager_id: str | None = None
    candidate_id: str
    title: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    # Nested relations (populated by service)
    coordinator: Coordinator | None = None
    client_contact: ClientContact | None = None
    recruiter: Contact | None = None
    client_manager: Contact | None = None
    candidate: Candidate | None = None
    stages: list[Stage] = []
    email_threads: list[EmailThread] = []

    @property
    def active_stages(self) -> list[Stage]:
        return [s for s in self.stages if s.is_active]

    @property
    def most_urgent_stage(self) -> Stage | None:
        active = self.active_stages
        if not active:
            return None
        return min(active, key=lambda s: (STATE_PRIORITY[s.state], s.ordinal))

    @property
    def computed_status(self) -> str:
        if not self.stages:
            return "empty"
        states = {s.state for s in self.stages}
        if states == {StageState.COMPLETE}:
            return "complete"
        if states == {StageState.COLD}:
            return "cold"
        if states <= {StageState.SCHEDULED, StageState.COMPLETE}:
            return "all_scheduled"
        if any(s.is_active for s in self.stages):
            return "active"
        return "mixed"


class LoopSummary(BaseModel):
    """Lightweight loop info for the status board."""

    loop_id: str
    title: str
    candidate_name: str
    client_company: str
    most_urgent_stage_id: str | None = None
    most_urgent_stage_name: str | None = None
    most_urgent_next_action: str | None = None
    most_urgent_state: StageState | None = None
    next_time_slot: TimeSlot | None = None


class StatusBoard(BaseModel):
    """Grouped loops for the homepage status board."""

    action_needed: list[LoopSummary] = []
    waiting: list[LoopSummary] = []
    scheduled: list[LoopSummary] = []
    complete: list[LoopSummary] = []
    cold: list[LoopSummary] = []
