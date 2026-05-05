"""DraftService — persists and manages email drafts.

Orchestrates the draft lifecycle: persistence → coordinator review →
send/discard. Recipient routing is deterministic from loop.state and
centralized in resolve_recipients() for DRY reuse across the codebase.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from psycopg.rows import dict_row, tuple_row

from api.drafts.models import DraftStatus, EmailDraft
from api.drafts.queries import queries
from api.ids import make_id
from api.scheduling.models import StageState

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from api.classifier.models import Suggestion
    from api.gmail.models import Message
    from api.scheduling.models import Loop
    from api.scheduling.service import LoopService


def _is_forward_draft(
    to_emails: list[str],
    thread_messages: list[Message] | None,
    trigger_message_id: str | None = None,
) -> bool:
    """A draft is a forward when the recipient wasn't on the triggering message."""
    if not thread_messages or not to_emails:
        return False

    trigger = None
    if trigger_message_id:
        trigger = next((m for m in thread_messages if m.id == trigger_message_id), None)
    if trigger is None:
        trigger = max(thread_messages, key=lambda m: m.date)

    seen: set[str] = {trigger.from_.email.lower()}
    for addr in trigger.to + trigger.cc:
        seen.add(addr.email.lower())

    return any(email.lower() not in seen for email in to_emails)


logger = logging.getLogger(__name__)

MAX_BODY_LENGTH = 2000


async def _collect(async_gen) -> list:
    return [row async for row in async_gen]


def _row_to_draft(row: dict) -> EmailDraft:
    """Convert a dict row (from psycopg dict_row factory) to an EmailDraft model."""
    raw_jit = row.get("pending_jit_data")
    if isinstance(raw_jit, str):
        row = {**row, "pending_jit_data": json.loads(raw_jit)}
    elif raw_jit is None:
        row = {**row, "pending_jit_data": {}}
    return EmailDraft(**row)


# ---------------------------------------------------------------------------
# Shared recipient routing — single source of truth
# ---------------------------------------------------------------------------


def resolve_recipients(
    loop: Loop,
    *,
    sender_email: str | None = None,
) -> tuple[list[str], list[str]]:
    """Determine to/cc emails from loop.state.

    Single source of truth for recipient routing. Both DraftService and the
    addon compose_email handler call this.

    ``sender_email`` (the coordinator) is filtered out of CC — coordinators
    are sometimes their own client manager, and CC'ing yourself is noise.
    """
    to_emails: list[str] = []
    cc_emails: list[str] = []

    state = loop.state

    if state == StageState.NEW:
        # NEW → email recruiter for availability
        if loop.recruiter and loop.recruiter.email:
            to_emails = [loop.recruiter.email]
    elif state in (StageState.AWAITING_CANDIDATE, StageState.AWAITING_CLIENT):
        # AWAITING_CANDIDATE/CLIENT → email client contact
        if loop.client_contact and loop.client_contact.email:
            to_emails = [loop.client_contact.email]
    elif state == StageState.SCHEDULED:
        # SCHEDULED → confirmation to client contact
        if loop.client_contact and loop.client_contact.email:
            to_emails = [loop.client_contact.email]
    else:
        # Fallback for COMPLETE/COLD (shouldn't normally draft here)
        if loop.client_contact and loop.client_contact.email:
            to_emails = [loop.client_contact.email]

    if loop.client_manager and loop.client_manager.email:
        cm_email = loop.client_manager.email
        if not sender_email or cm_email.lower() != sender_email.lower():
            cc_emails = [cm_email]

    return to_emails, cc_emails


class DraftService:
    """Persists and manages email drafts for scheduling communications."""

    def __init__(
        self,
        *,
        db_pool: AsyncConnectionPool,
        loop_service: LoopService,
    ):
        self._pool = db_pool
        self._loops = loop_service

    # ------------------------------------------------------------------
    # Draft creation
    # ------------------------------------------------------------------

    async def generate_draft(
        self,
        *,
        suggestion: Suggestion,
        loop: Loop,
        thread_messages: list[Message] | None = None,
        body: str = "",
    ) -> EmailDraft:
        """Create and persist an email draft for a DRAFT_EMAIL suggestion."""
        to_emails, cc_emails = resolve_recipients(loop, sender_email=suggestion.coordinator_email)
        subject = self._resolve_subject(loop)

        if len(body) > MAX_BODY_LENGTH:
            logger.warning(
                "draft body too long (%d chars) for suggestion %s — truncating",
                len(body),
                suggestion.id,
            )
            body = body[:MAX_BODY_LENGTH] + "\n\n[Draft truncated — please review]"

        is_forward = _is_forward_draft(to_emails, thread_messages, suggestion.gmail_message_id)

        draft_id = make_id("drf")
        async with self._pool.connection() as conn, conn.transaction():
            conn.row_factory = dict_row
            try:
                row = await queries.create_draft(
                    conn,
                    id=draft_id,
                    suggestion_id=suggestion.id,
                    loop_id=loop.id,
                    coordinator_email=suggestion.coordinator_email,
                    to_emails=to_emails,
                    cc_emails=cc_emails,
                    subject=subject,
                    body=body,
                    gmail_thread_id=suggestion.gmail_thread_id,
                    is_forward=is_forward,
                    status=DraftStatus.GENERATED,
                )
                draft = _row_to_draft(row)
            finally:
                conn.row_factory = tuple_row

        logger.info(
            "draft created: %s (suggestion=%s, loop=%s, to=%s, body_len=%d)",
            draft.id,
            suggestion.id,
            loop.id,
            to_emails,
            len(body),
        )
        return draft

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get_draft(self, draft_id: str) -> EmailDraft | None:
        async with self._pool.connection() as conn:
            conn.row_factory = dict_row
            try:
                row = await queries.get_draft(conn, id=draft_id)
                return _row_to_draft(row) if row else None
            finally:
                conn.row_factory = tuple_row

    async def get_draft_for_suggestion(self, suggestion_id: str) -> EmailDraft | None:
        async with self._pool.connection() as conn:
            conn.row_factory = dict_row
            try:
                row = await queries.get_draft_for_suggestion(conn, suggestion_id=suggestion_id)
                return _row_to_draft(row) if row else None
            finally:
                conn.row_factory = tuple_row

    async def get_pending_drafts(self, coordinator_email: str) -> list[EmailDraft]:
        async with self._pool.connection() as conn:
            conn.row_factory = dict_row
            try:
                rows = await _collect(
                    queries.get_pending_drafts_for_coordinator(
                        conn, coordinator_email=coordinator_email
                    )
                )
                return [_row_to_draft(r) for r in rows]
            finally:
                conn.row_factory = tuple_row

    async def update_draft_body(self, draft_id: str, body: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.update_draft_body(conn, id=draft_id, body=body)

    async def update_draft_recipients(
        self, draft_id: str, to_emails: list[str], cc_emails: list[str]
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.update_draft_recipients(
                conn, id=draft_id, to_emails=to_emails, cc_emails=cc_emails
            )

    async def update_pending_jit_data(self, draft_id: str, data: dict) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.update_pending_jit_data(
                conn, id=draft_id, pending_jit_data=json.dumps(data)
            )

    async def mark_sent(self, draft_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.mark_draft_sent(conn, id=draft_id)

    async def mark_discarded(self, draft_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.mark_draft_discarded(conn, id=draft_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_subject(loop: Loop) -> str:
        return f"Re: {loop.title}"
