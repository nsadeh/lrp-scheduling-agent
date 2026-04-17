"""Scheduling loop management service.

Orchestrates loop CRUD, stage state machine, event recording,
contact management, and email sending.
"""

from __future__ import annotations

import json
import os
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

import sentry_sdk

from api.ids import make_id
from api.scheduling.models import (
    ALLOWED_TRANSITIONS,
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    EmailThread,
    EventType,
    Loop,
    LoopEvent,
    LoopSummary,
    Stage,
    StageState,
    StatusBoard,
    TimeSlot,
)
from api.scheduling.queries import queries

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    from api.gmail.client import GmailClient


async def _collect(async_gen) -> list:
    """Collect all rows from an aiosql async generator into a list."""
    return [row async for row in async_gen]


def _email_domain(address: str) -> str:
    """Return the lowercase domain from an email address, or empty string."""
    _, _, domain = address.rpartition("@")
    return domain.strip().lower()


def _classify_recipients(
    *, recipients: list[str], coordinator_email: str
) -> tuple[list[str], bool]:
    """Return (unique recipient domains, is_internal).

    A send is internal when every recipient's domain is in
    ``INTERNAL_EMAIL_DOMAINS`` (comma-separated env var). When the env var is
    unset we fall back to the coordinator's own domain — coordinators are
    always LRP employees, so this is a safe default for reporting.
    """
    configured = os.environ.get("INTERNAL_EMAIL_DOMAINS", "")
    internal = {d.strip().lower() for d in configured.split(",") if d.strip()}
    if not internal:
        fallback = _email_domain(coordinator_email)
        if fallback:
            internal = {fallback}

    domains: list[str] = []
    seen: set[str] = set()
    for address in recipients:
        domain = _email_domain(address)
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)

    is_internal = bool(domains) and all(d in internal for d in domains)
    return domains, is_internal


class InvalidTransitionError(Exception):
    """Raised when a stage state transition is not allowed."""


class LoopService:
    def __init__(self, db_pool: AsyncConnectionPool, gmail: GmailClient | None = None):
        self._pool = db_pool
        self._gmail = gmail

    # ------------------------------------------------------------------
    # Coordinators
    # ------------------------------------------------------------------

    async def get_or_create_coordinator(self, name: str, email: str) -> Coordinator:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.get_or_create_coordinator(
                conn, id=make_id("crd"), name=name, email=email
            )
            return Coordinator(id=row[0], name=row[1], email=row[2], created_at=row[3])

    async def get_coordinator_by_email(self, email: str) -> Coordinator | None:
        async with self._pool.connection() as conn:
            row = await queries.get_coordinator_by_email(conn, email=email)
            if row is None:
                return None
            return Coordinator(id=row[0], name=row[1], email=row[2], created_at=row[3])

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    async def find_or_create_contact(
        self, name: str, email: str, role: str, company: str | None = None
    ) -> Contact:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.create_contact(
                conn, id=make_id("con"), name=name, email=email, role=role, company=company
            )
            return _row_to_contact(row)

    async def find_or_create_client_contact(
        self, name: str, email: str, company: str
    ) -> ClientContact:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.create_client_contact(
                conn, id=make_id("cli"), name=name, email=email, company=company
            )
            return _row_to_client_contact(row)

    async def find_or_create_candidate(self, name: str, notes: str | None = None) -> Candidate:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.create_candidate(conn, id=make_id("can"), name=name, notes=notes)
            return _row_to_candidate(row)

    async def search_contacts(self, prefix: str, role: str | None = None) -> list[Contact]:
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.search_contacts_by_prefix(conn, pattern=f"{prefix}%", role=role)
            )
            return [_row_to_contact(r) for r in rows]

    async def search_client_contacts(self, prefix: str) -> list[ClientContact]:
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.search_client_contacts_by_prefix(conn, pattern=f"{prefix}%")
            )
            return [_row_to_client_contact(r) for r in rows]

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def create_loop(
        self,
        coordinator_email: str,
        coordinator_name: str,
        candidate_name: str,
        client_contact_id: str,
        recruiter_id: str,
        title: str,
        first_stage_name: str = "Round 1",
        client_manager_id: str | None = None,
        gmail_thread_id: str | None = None,
        gmail_subject: str | None = None,
        notes: str | None = None,
    ) -> Loop:
        """Create a loop with its first stage, recording all events in one transaction."""
        async with self._pool.connection() as conn, conn.transaction():
            # Ensure coordinator exists
            coord_row = await queries.get_or_create_coordinator(
                conn, id=make_id("crd"), name=coordinator_name, email=coordinator_email
            )
            coordinator_id = coord_row[0]

            # Create candidate
            cand_row = await queries.create_candidate(
                conn, id=make_id("can"), name=candidate_name, notes=None
            )
            candidate_id = cand_row[0]

            # Create loop
            loop_id = make_id("lop")
            await queries.create_loop(
                conn,
                id=loop_id,
                coordinator_id=coordinator_id,
                client_contact_id=client_contact_id,
                recruiter_id=recruiter_id,
                client_manager_id=client_manager_id,
                candidate_id=candidate_id,
                title=title,
                notes=notes,
            )

            # Record loop_created event
            await self._record_event(
                conn,
                loop_id=loop_id,
                stage_id=None,
                event_type=EventType.LOOP_CREATED,
                data={"title": title, "candidate_name": candidate_name},
                actor_email=coordinator_email,
            )

            # Create first stage
            stage_id = make_id("stg")
            await queries.create_stage(
                conn,
                id=stage_id,
                loop_id=loop_id,
                name=first_stage_name,
                state=StageState.NEW,
                ordinal=0,
            )

            # Record stage_created event
            await self._record_event(
                conn,
                loop_id=loop_id,
                stage_id=stage_id,
                event_type=EventType.STAGE_CREATED,
                data={"name": first_stage_name, "ordinal": 0},
                actor_email=coordinator_email,
            )

            # Link email thread if provided
            if gmail_thread_id:
                await queries.link_thread(
                    conn,
                    id=make_id("let"),
                    loop_id=loop_id,
                    gmail_thread_id=gmail_thread_id,
                    subject=gmail_subject,
                )
                await self._record_event(
                    conn,
                    loop_id=loop_id,
                    stage_id=None,
                    event_type=EventType.THREAD_LINKED,
                    data={
                        "gmail_thread_id": gmail_thread_id,
                        "subject": gmail_subject,
                    },
                    actor_email=coordinator_email,
                )

        return await self.get_loop(loop_id)

    async def get_loop(self, loop_id: str) -> Loop:
        """Get a fully populated loop with all nested relations."""
        async with self._pool.connection() as conn:
            loop_row = await queries.get_loop(conn, id=loop_id)
            if loop_row is None:
                raise ValueError(f"Loop not found: {loop_id}")

            loop = _row_to_loop(loop_row)

            # Populate actors
            loop.coordinator = await self._get_coordinator(conn, loop.coordinator_id)
            loop.client_contact = await self._get_client_contact(conn, loop.client_contact_id)
            loop.recruiter = await self._get_contact(conn, loop.recruiter_id)
            if loop.client_manager_id:
                loop.client_manager = await self._get_contact(conn, loop.client_manager_id)
            loop.candidate = await self._get_candidate(conn, loop.candidate_id)

            # Populate stages with time slots
            stage_rows = await _collect(queries.get_stages_for_loop(conn, loop_id=loop_id))
            stages = [_row_to_stage(r) for r in stage_rows]
            for stage in stages:
                ts_rows = await _collect(queries.get_time_slots_for_stage(conn, stage_id=stage.id))
                stage.time_slots = [_row_to_time_slot(r) for r in ts_rows]
            loop.stages = stages

            # Populate email threads
            thread_rows = await _collect(queries.get_threads_for_loop(conn, loop_id=loop_id))
            loop.email_threads = [_row_to_email_thread(r) for r in thread_rows]

            return loop

    async def get_status_board(self, coordinator_email: str) -> StatusBoard:
        """Build the status board for a coordinator."""
        coord = await self.get_coordinator_by_email(coordinator_email)
        if coord is None:
            return StatusBoard()

        async with self._pool.connection() as conn:
            loop_rows = await _collect(
                queries.get_all_loops_for_coordinator(conn, coordinator_id=coord.id)
            )

        board = StatusBoard()
        for loop_row in loop_rows:
            loop = await self.get_loop(loop_row[0])
            summary = _loop_to_summary(loop)

            status = loop.computed_status
            if status == "complete":
                board.complete.append(summary)
            elif status == "cold":
                board.cold.append(summary)
            elif status == "all_scheduled":
                board.scheduled.append(summary)
            elif summary.most_urgent_state in (StageState.NEW,):
                board.action_needed.append(summary)
            elif summary.most_urgent_state in (
                StageState.AWAITING_CANDIDATE,
                StageState.AWAITING_CLIENT,
            ):
                board.waiting.append(summary)
            elif summary.most_urgent_state == StageState.SCHEDULED:
                board.scheduled.append(summary)
            else:
                board.action_needed.append(summary)

        return board

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    async def add_stage(self, loop_id: str, name: str, coordinator_email: str) -> Stage:
        async with self._pool.connection() as conn, conn.transaction():
            max_ord = await queries.get_max_ordinal_for_loop(conn, loop_id=loop_id)
            ordinal = (max_ord or 0) + 1

            stage_id = make_id("stg")
            row = await queries.create_stage(
                conn,
                id=stage_id,
                loop_id=loop_id,
                name=name,
                state=StageState.NEW,
                ordinal=ordinal,
            )
            await self._record_event(
                conn,
                loop_id=loop_id,
                stage_id=stage_id,
                event_type=EventType.STAGE_CREATED,
                data={"name": name, "ordinal": ordinal},
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=loop_id)
            return _row_to_stage(row)

    async def advance_stage(
        self,
        stage_id: str,
        to_state: StageState,
        coordinator_email: str,
        triggered_by: str | None = None,
    ) -> Stage:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.get_stage(conn, id=stage_id)
            if row is None:
                raise ValueError(f"Stage not found: {stage_id}")

            stage = _row_to_stage(row)
            from_state = stage.state
            _validate_transition(from_state, to_state)

            await queries.update_stage_state(conn, id=stage_id, state=to_state)
            await self._record_event(
                conn,
                loop_id=stage.loop_id,
                stage_id=stage_id,
                event_type=EventType.STAGE_ADVANCED,
                data={
                    "from_state": from_state,
                    "to_state": to_state,
                    "triggered_by": triggered_by,
                },
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=stage.loop_id)

            return stage.model_copy(update={"state": to_state})

    async def mark_cold(
        self, stage_id: str, coordinator_email: str, reason: str | None = None
    ) -> Stage:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.get_stage(conn, id=stage_id)
            if row is None:
                raise ValueError(f"Stage not found: {stage_id}")

            stage = _row_to_stage(row)
            from_state = stage.state
            _validate_transition(from_state, StageState.COLD)

            await queries.update_stage_state(conn, id=stage_id, state=StageState.COLD)
            await self._record_event(
                conn,
                loop_id=stage.loop_id,
                stage_id=stage_id,
                event_type=EventType.STAGE_MARKED_COLD,
                data={"from_state": from_state, "reason": reason},
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=stage.loop_id)

            return stage.model_copy(update={"state": StageState.COLD})

    async def revive_stage(
        self, stage_id: str, to_state: StageState, coordinator_email: str
    ) -> Stage:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.get_stage(conn, id=stage_id)
            if row is None:
                raise ValueError(f"Stage not found: {stage_id}")

            stage = _row_to_stage(row)
            if stage.state != StageState.COLD:
                raise InvalidTransitionError(f"Can only revive from cold, stage is {stage.state}")
            _validate_transition(StageState.COLD, to_state)

            await queries.update_stage_state(conn, id=stage_id, state=to_state)
            await self._record_event(
                conn,
                loop_id=stage.loop_id,
                stage_id=stage_id,
                event_type=EventType.STAGE_REVIVED,
                data={"to_state": to_state},
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=stage.loop_id)

            return stage.model_copy(update={"state": to_state})

    # ------------------------------------------------------------------
    # Email threads
    # ------------------------------------------------------------------

    async def link_thread(
        self,
        loop_id: str,
        gmail_thread_id: str,
        subject: str | None,
        coordinator_email: str,
    ) -> EmailThread | None:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.link_thread(
                conn,
                id=make_id("let"),
                loop_id=loop_id,
                gmail_thread_id=gmail_thread_id,
                subject=subject,
            )
            if row is None:
                return None  # already linked
            await self._record_event(
                conn,
                loop_id=loop_id,
                stage_id=None,
                event_type=EventType.THREAD_LINKED,
                data={"gmail_thread_id": gmail_thread_id, "subject": subject},
                actor_email=coordinator_email,
            )
            return _row_to_email_thread(row)

    async def find_loop_by_thread(self, gmail_thread_id: str) -> Loop | None:
        async with self._pool.connection() as conn:
            row = await queries.find_loop_by_gmail_thread_id(conn, gmail_thread_id=gmail_thread_id)
            if row is None:
                return None
            return await self.get_loop(row[0])

    # ------------------------------------------------------------------
    # Email sending
    # ------------------------------------------------------------------

    async def send_email(
        self,
        loop_id: str,
        stage_id: str,
        coordinator_email: str,
        to: list[str],
        subject: str,
        body: str,
        gmail_thread_id: str | None = None,
        in_reply_to: str | None = None,
        auto_advance_to: StageState | None = None,
    ) -> None:
        """Send an email via Gmail and record the event. Optionally advance the stage."""
        recipient_domains, is_internal = _classify_recipients(
            recipients=to, coordinator_email=coordinator_email
        )
        scope = sentry_sdk.Scope.get_current_scope()
        scope.set_tag("email.is_internal", is_internal)
        scope.set_tag("email.recipient_domain", ",".join(recipient_domains))

        gmail_message_id = None
        if self._gmail:
            sent = await self._gmail.send_message(
                user_email=coordinator_email,
                to=to,
                subject=subject,
                body=body,
                thread_id=gmail_thread_id,
                in_reply_to=in_reply_to,
            )
            gmail_message_id = sent.id

        async with self._pool.connection() as conn, conn.transaction():
            await self._record_event(
                conn,
                loop_id=loop_id,
                stage_id=stage_id,
                event_type=EventType.EMAIL_SENT,
                data={
                    "to": to,
                    "subject": subject,
                    "gmail_message_id": gmail_message_id,
                    "gmail_thread_id": gmail_thread_id,
                    "recipient_domains": recipient_domains,
                    "is_internal": is_internal,
                },
                actor_email=coordinator_email,
            )

            if auto_advance_to:
                row = await queries.get_stage(conn, id=stage_id)
                if row:
                    stage = _row_to_stage(row)
                    if auto_advance_to in ALLOWED_TRANSITIONS.get(stage.state, set()):
                        await queries.update_stage_state(conn, id=stage_id, state=auto_advance_to)
                        await self._record_event(
                            conn,
                            loop_id=loop_id,
                            stage_id=stage_id,
                            event_type=EventType.STAGE_ADVANCED,
                            data={
                                "from_state": stage.state,
                                "to_state": auto_advance_to,
                                "triggered_by": "email_sent",
                            },
                            actor_email=coordinator_email,
                        )

            await queries.update_loop_timestamp(conn, id=loop_id)

    # ------------------------------------------------------------------
    # Time slots
    # ------------------------------------------------------------------

    async def add_time_slot(
        self,
        stage_id: str,
        start_time: datetime,
        duration_minutes: int,
        timezone: str,
        coordinator_email: str,
        zoom_link: str | None = None,
        notes: str | None = None,
    ) -> TimeSlot:
        async with self._pool.connection() as conn, conn.transaction():
            # Look up stage to get loop_id
            stage_row = await queries.get_stage(conn, id=stage_id)
            if stage_row is None:
                raise ValueError(f"Stage not found: {stage_id}")
            loop_id = stage_row[1]

            ts_id = make_id("tms")
            row = await queries.create_time_slot(
                conn,
                id=ts_id,
                stage_id=stage_id,
                start_time=start_time,
                duration_minutes=duration_minutes,
                timezone=timezone,
                zoom_link=zoom_link,
                notes=notes,
            )
            await self._record_event(
                conn,
                loop_id=loop_id,
                stage_id=stage_id,
                event_type=EventType.TIME_SLOT_ADDED,
                data={
                    "time_slot_id": ts_id,
                    "start_time": start_time.isoformat(),
                    "duration_minutes": duration_minutes,
                    "timezone": timezone,
                },
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=loop_id)
            return _row_to_time_slot(row)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def get_events(self, loop_id: str, stage_id: str | None = None) -> list[LoopEvent]:
        async with self._pool.connection() as conn:
            if stage_id:
                rows = await _collect(queries.get_events_for_stage(conn, stage_id=stage_id))
            else:
                rows = await _collect(queries.get_events_for_loop(conn, loop_id=loop_id))
            return [_row_to_event(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_event(
        self,
        conn: AsyncConnection,
        loop_id: str,
        stage_id: str | None,
        event_type: EventType,
        data: dict[str, Any],
        actor_email: str,
    ) -> None:
        await queries.insert_event(
            conn,
            id=make_id("evt"),
            loop_id=loop_id,
            stage_id=stage_id,
            event_type=event_type,
            data=json.dumps(data),
            actor_email=actor_email,
        )

    async def _get_coordinator(self, conn: AsyncConnection, entity_id: str) -> Coordinator | None:
        row = await queries.get_coordinator(conn, id=entity_id)
        if row is None:
            return None
        return Coordinator(id=row[0], name=row[1], email=row[2], created_at=row[3])

    async def _get_contact(self, conn: AsyncConnection, entity_id: str) -> Contact | None:
        row = await queries.get_contact(conn, id=entity_id)
        return _row_to_contact(row) if row else None

    async def _get_client_contact(
        self, conn: AsyncConnection, entity_id: str
    ) -> ClientContact | None:
        row = await queries.get_client_contact(conn, id=entity_id)
        return _row_to_client_contact(row) if row else None

    async def _get_candidate(self, conn: AsyncConnection, entity_id: str) -> Candidate | None:
        row = await queries.get_candidate(conn, id=entity_id)
        return _row_to_candidate(row) if row else None


# ======================================================================
# Row → model converters
# ======================================================================


def _row_to_contact(row: tuple) -> Contact:
    return Contact(
        id=row[0], name=row[1], email=row[2], role=row[3], company=row[4], created_at=row[5]
    )


def _row_to_client_contact(row: tuple) -> ClientContact:
    return ClientContact(id=row[0], name=row[1], email=row[2], company=row[3], created_at=row[4])


def _row_to_candidate(row: tuple) -> Candidate:
    return Candidate(id=row[0], name=row[1], notes=row[2], created_at=row[3])


def _row_to_stage(row: tuple) -> Stage:
    return Stage(
        id=row[0],
        loop_id=row[1],
        name=row[2],
        state=row[3],
        ordinal=row[4],
        created_at=row[5],
        updated_at=row[6],
    )


def _row_to_loop(row: tuple) -> Loop:
    return Loop(
        id=row[0],
        coordinator_id=row[1],
        client_contact_id=row[2],
        recruiter_id=row[3],
        client_manager_id=row[4],
        candidate_id=row[5],
        title=row[6],
        notes=row[7],
        created_at=row[8],
        updated_at=row[9],
    )


def _row_to_event(row: tuple) -> LoopEvent:
    data = row[4]
    if isinstance(data, str):
        data = json.loads(data)
    return LoopEvent(
        id=row[0],
        loop_id=row[1],
        stage_id=row[2],
        event_type=row[3],
        data=data,
        actor_email=row[5],
        occurred_at=row[6],
    )


def _row_to_email_thread(row: tuple) -> EmailThread:
    return EmailThread(
        id=row[0],
        loop_id=row[1],
        gmail_thread_id=row[2],
        subject=row[3],
        linked_at=row[4],
    )


def _row_to_time_slot(row: tuple) -> TimeSlot:
    return TimeSlot(
        id=row[0],
        stage_id=row[1],
        start_time=row[2],
        duration_minutes=row[3],
        timezone=row[4],
        zoom_link=row[5],
        notes=row[6],
        created_at=row[7],
    )


def _loop_to_summary(loop: Loop) -> LoopSummary:
    urgent = loop.most_urgent_stage
    next_ts = None
    if urgent and urgent.time_slots:
        next_ts = urgent.time_slots[0]

    return LoopSummary(
        loop_id=loop.id,
        title=loop.title,
        candidate_name=loop.candidate.name if loop.candidate else "Unknown",
        client_company=loop.client_contact.company if loop.client_contact else "Unknown",
        most_urgent_stage_id=urgent.id if urgent else None,
        most_urgent_stage_name=urgent.name if urgent else None,
        most_urgent_next_action=urgent.next_action if urgent else None,
        most_urgent_state=urgent.state if urgent else None,
        next_time_slot=next_ts,
    )


def _validate_transition(from_state: StageState, to_state: StageState) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition from {from_state} to {to_state}. Allowed: {sorted(allowed)}"
        )
