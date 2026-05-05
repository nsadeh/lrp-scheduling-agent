"""Scheduling loop management service.

Orchestrates loop CRUD, state transitions, event recording,
contact management, and email sending.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import sentry_sdk
from psycopg.rows import dict_row

from api.ids import make_id
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    EmailThread,
    EventType,
    Loop,
    LoopEvent,
    LoopSummary,
    StageState,
    StatusBoard,
)
from api.scheduling.queries import queries

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    from api.gmail.client import GmailClient


async def _collect(async_gen) -> list:
    """Collect all rows from an aiosql async generator into a list."""
    return [row async for row in async_gen]


async def _fetch_dicts(conn, query, **params) -> list[dict]:
    """Execute an aiosql query and return rows as dicts (named-column access)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query.sql, params)
        return await cur.fetchall()


async def _fetch_dict_one(conn, query, **params) -> dict | None:
    """Single-row variant of `_fetch_dicts`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query.sql, params)
        return await cur.fetchone()


def _email_domain(address: str) -> str:
    _, _, domain = address.rpartition("@")
    return domain.strip().lower()


def _classify_recipients(
    *, recipients: list[str], coordinator_email: str
) -> tuple[list[str], bool]:
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
    """Raised when revive() is called on a non-cold loop."""


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
        self,
        name: str,
        email: str,
        role: str,
        company: str | None = None,
        photo_url: str | None = None,
    ) -> Contact:
        async with self._pool.connection() as conn, conn.transaction():
            existing = await queries.get_contact_by_email_and_role(conn, email=email, role=role)
            if existing is not None:
                return _row_to_contact(existing)
            row = await queries.create_contact(
                conn,
                id=make_id("con"),
                name=name,
                email=email,
                role=role,
                company=company,
                photo_url=photo_url,
            )
            return _row_to_contact(row)

    async def find_or_create_client_contact(
        self, name: str, email: str, company: str | None = None
    ) -> ClientContact:
        async with self._pool.connection() as conn, conn.transaction():
            existing = await queries.get_client_contact_by_email(conn, email=email)
            if existing is not None:
                return _row_to_client_contact(existing)
            row = await queries.create_client_contact(
                conn, id=make_id("cli"), name=name, email=email, company=company
            )
            return _row_to_client_contact(row)

    async def get_contact_by_email(self, email: str, role: str) -> Contact | None:
        async with self._pool.connection() as conn:
            row = await queries.get_contact_by_email_and_role(conn, email=email, role=role)
            if row is None:
                return None
            return _row_to_contact(row)

    async def get_client_contact_by_email(self, email: str) -> ClientContact | None:
        async with self._pool.connection() as conn:
            row = await queries.get_client_contact_by_email(conn, email=email)
            if row is None:
                return None
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
        client_contact_id: str | None,
        recruiter_id: str | None,
        title: str,
        client_manager_id: str | None = None,
        gmail_thread_id: str | None = None,
        gmail_subject: str | None = None,
        notes: str | None = None,
    ) -> Loop:
        """Create a loop in the NEW state, recording the create event."""
        async with self._pool.connection() as conn, conn.transaction():
            coord_row = await queries.get_or_create_coordinator(
                conn, id=make_id("crd"), name=coordinator_name, email=coordinator_email
            )
            coordinator_id = coord_row[0]

            cand_row = await queries.create_candidate(
                conn, id=make_id("can"), name=candidate_name, notes=None
            )
            candidate_id = cand_row[0]

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
                state=StageState.NEW,
                notes=notes,
            )

            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.LOOP_CREATED,
                data={"title": title, "candidate_name": candidate_name},
                actor_email=coordinator_email,
            )

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
                    event_type=EventType.THREAD_LINKED,
                    data={
                        "gmail_thread_id": gmail_thread_id,
                        "subject": gmail_subject,
                    },
                    actor_email=coordinator_email,
                )

        return await self.get_loop(loop_id)

    async def get_loop(self, loop_id: str) -> Loop:
        """Get a fully populated loop with actor relations and email threads (2 queries)."""
        async with self._pool.connection() as conn:
            loop_row = await _fetch_dict_one(conn, queries.get_loop_full, id=loop_id)
            if loop_row is None:
                raise ValueError(f"Loop not found: {loop_id}")

            loop = _row_to_loop_full(loop_row)

            thread_rows = await _collect(queries.get_threads_for_loop(conn, loop_id=loop_id))
            loop.email_threads = [_row_to_email_thread(r) for r in thread_rows]

            return loop

    async def _hydrate_loop_relations(self, loops: list[Loop]) -> list[Loop]:
        """Populate email threads on actor-populated Loops via a single batch query."""
        if not loops:
            return loops

        loop_ids = [loop.id for loop in loops]

        async with self._pool.connection() as conn:
            thread_rows = await _collect(queries.get_threads_for_loops(conn, loop_ids=loop_ids))

        threads_by_loop: dict[str, list[EmailThread]] = {}
        for r in thread_rows:
            et = _row_to_email_thread(r)
            threads_by_loop.setdefault(et.loop_id, []).append(et)

        for loop in loops:
            loop.email_threads = threads_by_loop.get(loop.id, [])

        return loops

    async def get_status_board(self, coordinator_email: str) -> StatusBoard:
        coord = await self.get_coordinator_by_email(coordinator_email)
        if coord is None:
            return StatusBoard()

        async with self._pool.connection() as conn:
            loop_rows = await _fetch_dicts(
                conn, queries.get_loops_full_for_coordinator, coordinator_id=coord.id
            )

        loops = await self._hydrate_loop_relations([_row_to_loop_full(r) for r in loop_rows])

        board = StatusBoard()
        for loop in loops:
            summary = _loop_to_summary(loop)

            if loop.state == StageState.COMPLETE:
                board.complete.append(summary)
            elif loop.state == StageState.COLD:
                board.cold.append(summary)
            elif loop.state == StageState.SCHEDULED:
                board.scheduled.append(summary)
            elif loop.state == StageState.NEW:
                board.action_needed.append(summary)
            elif loop.state in (StageState.AWAITING_CANDIDATE, StageState.AWAITING_CLIENT):
                board.waiting.append(summary)
            else:
                board.action_needed.append(summary)

        return board

    # ------------------------------------------------------------------
    # State transitions (operate on loops directly)
    # ------------------------------------------------------------------

    async def advance_state(
        self,
        loop_id: str,
        to_state: StageState,
        coordinator_email: str,
        triggered_by: str | None = None,
    ) -> Loop:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.get_loop(conn, id=loop_id)
            if row is None:
                raise ValueError(f"Loop not found: {loop_id}")
            from_state = row[8]  # see _row_to_loop column ordering

            await queries.update_loop_state(conn, id=loop_id, state=to_state)
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.STATE_ADVANCED,
                data={
                    "from_state": from_state,
                    "to_state": to_state,
                    "triggered_by": triggered_by,
                },
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=loop_id)

        return await self.get_loop(loop_id)

    async def mark_cold(
        self, loop_id: str, coordinator_email: str, reason: str | None = None
    ) -> Loop:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.get_loop(conn, id=loop_id)
            if row is None:
                raise ValueError(f"Loop not found: {loop_id}")
            from_state = row[8]

            await queries.update_loop_state(conn, id=loop_id, state=StageState.COLD)
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.LOOP_MARKED_COLD,
                data={"from_state": from_state, "reason": reason},
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=loop_id)

        return await self.get_loop(loop_id)

    async def revive(self, loop_id: str, to_state: StageState, coordinator_email: str) -> Loop:
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.get_loop(conn, id=loop_id)
            if row is None:
                raise ValueError(f"Loop not found: {loop_id}")
            from_state = row[8]
            if from_state != StageState.COLD:
                raise InvalidTransitionError(f"Can only revive from cold, loop is {from_state}")

            await queries.update_loop_state(conn, id=loop_id, state=to_state)
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.LOOP_REVIVED,
                data={"to_state": to_state},
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=loop_id)

        return await self.get_loop(loop_id)

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

    async def find_loops_by_thread(self, gmail_thread_id: str) -> list[Loop]:
        """All loops linked to a Gmail thread (multi-loop threads supported)."""
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.find_loops_by_gmail_thread_id(conn, gmail_thread_id=gmail_thread_id)
            )
        return [await self.get_loop(row[0]) for row in rows]

    # ------------------------------------------------------------------
    # JIT actor patching
    # ------------------------------------------------------------------

    async def set_recruiter(self, loop_id: str, recruiter_id: str, coordinator_email: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.set_loop_recruiter(conn, id=loop_id, recruiter_id=recruiter_id)
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.ACTOR_UPDATED,
                data={"actor": "recruiter", "recruiter_id": recruiter_id},
                actor_email=coordinator_email,
            )

    async def set_client_contact(
        self, loop_id: str, client_contact_id: str, coordinator_email: str
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.set_loop_client_contact(
                conn, id=loop_id, client_contact_id=client_contact_id
            )
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.ACTOR_UPDATED,
                data={"actor": "client_contact", "client_contact_id": client_contact_id},
                actor_email=coordinator_email,
            )

    async def set_client_manager(
        self, loop_id: str, client_manager_id: str, coordinator_email: str
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.set_loop_client_manager(
                conn, id=loop_id, client_manager_id=client_manager_id
            )
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.ACTOR_UPDATED,
                data={"actor": "client_manager", "client_manager_id": client_manager_id},
                actor_email=coordinator_email,
            )

    async def update_candidate_name(
        self, candidate_id: str, name: str, coordinator_email: str, loop_id: str
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.update_candidate_name(conn, id=candidate_id, name=name)
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.ACTOR_UPDATED,
                data={"actor": "candidate", "name": name},
                actor_email=coordinator_email,
            )
            await queries.update_loop_timestamp(conn, id=loop_id)

    # ------------------------------------------------------------------
    # Email sending
    # ------------------------------------------------------------------

    async def send_email(
        self,
        loop_id: str,
        coordinator_email: str,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        gmail_thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> None:
        """Send an email via Gmail and record the event."""
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
                cc=cc,
                thread_id=gmail_thread_id,
                in_reply_to=in_reply_to,
                references=references,
            )
            gmail_message_id = sent.id

        async with self._pool.connection() as conn, conn.transaction():
            await self._record_event(
                conn,
                loop_id=loop_id,
                event_type=EventType.EMAIL_SENT,
                data={
                    "to": to,
                    "cc": cc or [],
                    "subject": subject,
                    "body": body,
                    "gmail_message_id": gmail_message_id,
                    "gmail_thread_id": gmail_thread_id,
                    "recipient_domains": recipient_domains,
                    "is_internal": is_internal,
                },
                actor_email=coordinator_email,
            )

            await queries.update_loop_timestamp(conn, id=loop_id)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def get_events(self, loop_id: str) -> list[LoopEvent]:
        async with self._pool.connection() as conn:
            rows = await _collect(queries.get_events_for_loop(conn, loop_id=loop_id))
            return [_row_to_event(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_event(
        self,
        conn: AsyncConnection,
        loop_id: str,
        event_type: EventType,
        data: dict[str, Any],
        actor_email: str,
    ) -> None:
        await queries.insert_event(
            conn,
            id=make_id("evt"),
            loop_id=loop_id,
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
        id=row[0],
        name=row[1],
        email=row[2],
        role=row[3],
        company=row[4],
        photo_url=row[5],
        created_at=row[6],
    )


def _row_to_client_contact(row: tuple) -> ClientContact:
    return ClientContact(id=row[0], name=row[1], email=row[2], company=row[3], created_at=row[4])


def _row_to_candidate(row: tuple) -> Candidate:
    return Candidate(id=row[0], name=row[1], notes=row[2], created_at=row[3])


def _row_to_loop(row: tuple) -> Loop:
    """Tuple shape: id, coordinator_id, client_contact_id, recruiter_id,
    client_manager_id, candidate_id, title, notes, state, created_at, updated_at."""
    return Loop(
        id=row[0],
        coordinator_id=row[1],
        client_contact_id=row[2],
        recruiter_id=row[3],
        client_manager_id=row[4],
        candidate_id=row[5],
        title=row[6],
        notes=row[7],
        state=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def _row_to_loop_full(row: dict) -> Loop:
    """Convert a get_loop_full / get_loops_full_* dict row into a populated Loop."""
    loop = Loop(
        id=row["loop_id"],
        coordinator_id=row["loop_coordinator_id"],
        client_contact_id=row["loop_client_contact_id"],
        recruiter_id=row["loop_recruiter_id"],
        client_manager_id=row["loop_client_manager_id"],
        candidate_id=row["loop_candidate_id"],
        title=row["loop_title"],
        notes=row["loop_notes"],
        state=row["loop_state"],
        created_at=row["loop_created_at"],
        updated_at=row["loop_updated_at"],
    )
    loop.coordinator = Coordinator(
        id=row["loop_coordinator_id"],
        name=row["coord_name"],
        email=row["coord_email"],
        created_at=row["coord_created_at"],
    )
    if row["cc_name"] is not None:
        loop.client_contact = ClientContact(
            id=row["loop_client_contact_id"],
            name=row["cc_name"],
            email=row["cc_email"],
            company=row["cc_company"],
            created_at=row["cc_created_at"],
        )
    if row["rec_name"] is not None:
        loop.recruiter = Contact(
            id=row["loop_recruiter_id"],
            name=row["rec_name"],
            email=row["rec_email"],
            role=row["rec_role"],
            company=row["rec_company"],
            photo_url=row["rec_photo_url"],
            created_at=row["rec_created_at"],
        )
    if row["cm_name"] is not None:
        loop.client_manager = Contact(
            id=row["loop_client_manager_id"],
            name=row["cm_name"],
            email=row["cm_email"],
            role=row["cm_role"],
            company=row["cm_company"],
            photo_url=row["cm_photo_url"],
            created_at=row["cm_created_at"],
        )
    loop.candidate = Candidate(
        id=row["loop_candidate_id"],
        name=row["cand_name"],
        notes=row["cand_notes"],
        created_at=row["cand_created_at"],
    )
    return loop


def _row_to_event(row: tuple) -> LoopEvent:
    """Tuple shape: id, loop_id, event_type, data, actor_email, occurred_at."""
    data = row[3]
    if isinstance(data, str):
        data = json.loads(data)
    return LoopEvent(
        id=row[0],
        loop_id=row[1],
        event_type=row[2],
        data=data,
        actor_email=row[4],
        occurred_at=row[5],
    )


def _row_to_email_thread(row: tuple) -> EmailThread:
    return EmailThread(
        id=row[0],
        loop_id=row[1],
        gmail_thread_id=row[2],
        subject=row[3],
        linked_at=row[4],
    )


def _loop_to_summary(loop: Loop) -> LoopSummary:
    client_company = "Unknown"
    if loop.client_contact and loop.client_contact.company:
        client_company = loop.client_contact.company
    return LoopSummary(
        loop_id=loop.id,
        title=loop.title,
        candidate_name=loop.candidate.name if loop.candidate else "Unknown",
        client_company=client_company,
        state=loop.state,
        next_action=loop.next_action,
    )
