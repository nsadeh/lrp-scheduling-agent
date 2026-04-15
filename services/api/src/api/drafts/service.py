"""DraftService — generates, persists, and manages AI email drafts.

Orchestrates the draft lifecycle: LLM generation → persistence → coordinator
review → send/discard. Recipient routing is deterministic from stage state,
matching the existing logic in addon/routes.py _handle_compose_email.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from api.classifier.formatters import format_loop_state, format_thread_history
from api.drafts.endpoint import generate_draft_content
from api.drafts.models import DraftOutput, DraftStatus, EmailDraft, GenerateDraftInput
from api.drafts.queries import queries
from api.ids import make_id
from api.scheduling.models import StageState

if TYPE_CHECKING:
    from langfuse import Langfuse
    from psycopg_pool import AsyncConnectionPool

    from api.ai.llm_service import LLMService
    from api.classifier.models import Suggestion
    from api.gmail.models import Message
    from api.scheduling.models import Loop, Stage
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

# Maximum generated body length before truncation (scheduling emails are short)
MAX_BODY_LENGTH = 2000


async def _collect(async_gen) -> list:
    return [row async for row in async_gen]


def _row_to_draft(row: tuple) -> EmailDraft:
    return EmailDraft(
        id=row[0],
        suggestion_id=row[1],
        loop_id=row[2],
        stage_id=row[3],
        coordinator_email=row[4],
        to_emails=row[5],
        cc_emails=row[6],
        subject=row[7],
        body=row[8],
        gmail_thread_id=row[9],
        status=row[10],
        sent_at=row[11],
        created_at=row[12],
        updated_at=row[13],
    )


class DraftService:
    """Generates and manages AI email drafts for scheduling communications."""

    def __init__(
        self,
        *,
        db_pool: AsyncConnectionPool,
        loop_service: LoopService,
        llm: LLMService | None = None,
        langfuse: Langfuse | None = None,
    ):
        self._pool = db_pool
        self._loops = loop_service
        self._llm = llm
        self._langfuse = langfuse

    # ------------------------------------------------------------------
    # Draft generation
    # ------------------------------------------------------------------

    async def generate_draft(
        self,
        *,
        suggestion: Suggestion,
        loop: Loop,
        thread_messages: list[Message] | None = None,
    ) -> EmailDraft:
        """Generate and persist an email draft for a DRAFT_EMAIL suggestion.

        On LLM failure, creates a draft with an empty body so the coordinator
        can compose manually from the sidebar.
        """
        stage = self._resolve_stage(loop, suggestion.stage_id)
        to_emails, cc_emails = self._resolve_recipients(loop, stage)
        subject = self._resolve_subject(loop)

        # Generate body via LLM (fallback to empty on failure)
        body = ""
        if self._llm and self._langfuse:
            try:
                llm_input = self._build_input(suggestion, loop, stage, thread_messages)
                result: DraftOutput = await generate_draft_content(
                    llm=self._llm,
                    langfuse=self._langfuse,
                    data=llm_input,
                )
                body = result.body

                # Guardrail: truncate overly long output
                if len(body) > MAX_BODY_LENGTH:
                    logger.warning(
                        "draft body too long (%d chars) for suggestion %s — truncating",
                        len(body),
                        suggestion.id,
                    )
                    body = body[:MAX_BODY_LENGTH] + "\n\n[Draft truncated — please review]"

            except Exception:
                logger.exception(
                    "draft LLM call failed for suggestion %s — creating empty draft",
                    suggestion.id,
                )

        # Persist
        draft_id = make_id("drf")
        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.create_draft(
                conn,
                id=draft_id,
                suggestion_id=suggestion.id,
                loop_id=loop.id,
                stage_id=stage.id if stage else loop.stages[0].id,
                coordinator_email=suggestion.coordinator_email,
                to_emails=to_emails,
                cc_emails=cc_emails,
                subject=subject,
                body=body,
                gmail_thread_id=suggestion.gmail_thread_id,
                status=DraftStatus.GENERATED,
            )
            draft = _row_to_draft(row)

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
            row = await queries.get_draft(conn, id=draft_id)
            return _row_to_draft(row) if row else None

    async def get_draft_for_suggestion(self, suggestion_id: str) -> EmailDraft | None:
        async with self._pool.connection() as conn:
            row = await queries.get_draft_for_suggestion(conn, suggestion_id=suggestion_id)
            return _row_to_draft(row) if row else None

    async def get_pending_drafts(self, coordinator_email: str) -> list[EmailDraft]:
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.get_pending_drafts_for_coordinator(
                    conn, coordinator_email=coordinator_email
                )
            )
            return [_row_to_draft(r) for r in rows]

    async def update_draft_body(self, draft_id: str, body: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.update_draft_body(conn, id=draft_id, body=body)

    async def mark_sent(self, draft_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.mark_draft_sent(conn, id=draft_id)

    async def mark_discarded(self, draft_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.mark_draft_discarded(conn, id=draft_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_stage(self, loop: Loop, stage_id: str | None) -> Stage | None:
        """Find the target stage for a draft."""
        if stage_id:
            for stage in loop.stages:
                if stage.id == stage_id:
                    return stage
        # Fallback: most urgent active stage
        return loop.most_urgent_stage

    def _resolve_recipients(
        self,
        loop: Loop,
        stage: Stage | None,
    ) -> tuple[list[str], list[str]]:
        """Determine to/cc emails from stage state.

        Mirrors the routing logic in addon/routes.py _handle_compose_email.
        """
        to_emails: list[str] = []
        cc_emails: list[str] = []

        state = stage.state if stage else StageState.NEW

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

        # Client manager is always CC'd when present
        if loop.client_manager and loop.client_manager.email:
            cc_emails = [loop.client_manager.email]

        return to_emails, cc_emails

    def _resolve_subject(self, loop: Loop) -> str:
        """Build a subject line for the draft — reply to the loop thread."""
        return f"Re: {loop.title}"

    def _build_input(
        self,
        suggestion: Suggestion,
        loop: Loop,
        stage: Stage | None,
        thread_messages: list[Message] | None,
    ) -> GenerateDraftInput:
        """Construct LLM input from suggestion context."""
        # Thread summary
        thread_summary = "No prior messages in this thread."
        if thread_messages:
            thread_summary = format_thread_history(
                thread_messages, current_message_id=suggestion.gmail_message_id
            )

        # Determine recipient type
        state = stage.state if stage else StageState.NEW
        recipient_type = "recruiter" if state == StageState.NEW else "client"

        # Resolve recipient name
        if recipient_type == "recruiter":
            recipient_name = loop.recruiter.name if loop.recruiter else "Recruiter"
        else:
            recipient_name = loop.client_contact.name if loop.client_contact else "Client"

        return GenerateDraftInput(
            classification=suggestion.classification,
            recipient_type=recipient_type,
            recipient_name=recipient_name,
            candidate_name=loop.candidate.name if loop.candidate else "Candidate",
            coordinator_name=loop.coordinator.name if loop.coordinator else "Coordinator",
            stage_state=state.value if isinstance(state, StageState) else str(state),
            extracted_entities=json.dumps(suggestion.extracted_entities),
            thread_summary=thread_summary,
            loop_context=format_loop_state(loop),
        )
