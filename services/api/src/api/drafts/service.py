"""DraftService — generates, persists, and manages AI email drafts.

Orchestrates the draft lifecycle: LLM generation → persistence → coordinator
review → send/discard. Recipient routing is deterministic from stage state
and centralized in resolve_recipients() for DRY reuse across the codebase.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from psycopg.rows import dict_row, tuple_row

from api.classifier.formatters import format_thread_history
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


def _is_forward_draft(to_emails: list[str], thread_messages: list[Message] | None) -> bool:
    """Determine if a draft is a forward by checking if any recipient is new to the thread.

    A draft is a forward when it targets someone who hasn't participated in the
    thread yet (not in any prior message's from/to/cc). If there are no prior
    messages to compare against, we assume it's not a forward.
    """
    if not thread_messages or not to_emails:
        return False

    # Build set of all participants from prior thread messages
    seen: set[str] = set()
    for msg in thread_messages:
        seen.add(msg.from_.email.lower())
        for addr in msg.to + msg.cc:
            seen.add(addr.email.lower())

    # If any recipient is not in the thread, it's a forward
    return any(email.lower() not in seen for email in to_emails)


logger = logging.getLogger(__name__)

# Maximum generated body length before truncation (scheduling emails are short)
MAX_BODY_LENGTH = 2000


async def _collect(async_gen) -> list:
    return [row async for row in async_gen]


def _row_to_draft(row: dict) -> EmailDraft:
    """Convert a dict row (from psycopg dict_row factory) to an EmailDraft model."""
    # JSONB columns may arrive as either dict (when the psycopg JSON
    # adapter is registered) or string (raw). Normalize so Pydantic
    # validation succeeds in both cases.
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
    stage: Stage | None,
    *,
    sender_email: str | None = None,
) -> tuple[list[str], list[str]]:
    """Determine to/cc emails from stage state.

    This is the single source of truth for recipient routing. Both the
    DraftService and the addon compose_email handler should call this.

    ``sender_email`` (the coordinator sending the message) is filtered
    out of CC — coordinators are sometimes their own client manager
    (e.g. Adam's loops where he is both coordinator and CM), and CC'ing
    yourself on your own send is noise.
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

    # Client manager is CC'd when present, but never CC the sender.
    if loop.client_manager and loop.client_manager.email:
        cm_email = loop.client_manager.email
        if not sender_email or cm_email.lower() != sender_email.lower():
            cc_emails = [cm_email]

    return to_emails, cc_emails


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
        to_emails, cc_emails = resolve_recipients(
            loop, stage, sender_email=suggestion.coordinator_email
        )
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

        # Determine if this is a forward: are we sending to someone new?
        is_forward = _is_forward_draft(to_emails, thread_messages)

        # Persist (using dict_row for clean dict→model conversion)
        draft_id = make_id("drf")
        async with self._pool.connection() as conn, conn.transaction():
            conn.row_factory = dict_row
            try:
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
        """Patch a draft's recipients after JIT contact info was supplied.

        Used by the send_draft handler when the loop was auto-created with
        a missing recruiter/client and the coordinator filled them in inline
        on the draft card.
        """
        async with self._pool.connection() as conn, conn.transaction():
            await queries.update_draft_recipients(
                conn, id=draft_id, to_emails=to_emails, cc_emails=cc_emails
            )

    async def update_pending_jit_data(self, draft_id: str, data: dict) -> None:
        """Replace pending_jit_data on the draft.

        Stores the coordinator's in-flight contact picks (recruiter / client
        / CM) until they click Send. Misclicks can be undone with the "x"
        clear button before commit.
        """
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

    def _resolve_stage(self, loop: Loop, stage_id: str | None) -> Stage | None:
        """Find the target stage for a draft."""
        if stage_id:
            for stage in loop.stages:
                if stage.id == stage_id:
                    return stage
        # Fallback: most urgent active stage
        return loop.most_urgent_stage

    @staticmethod
    def _resolve_subject(loop: Loop) -> str:
        """Build a subject line for the draft — reply to the loop thread."""
        return f"Re: {loop.title}"

    @staticmethod
    def _resolve_recipient_name(loop: Loop, stage: Stage | None) -> str:
        """Get the first name of the recipient for the drafter prompt."""
        state = stage.state if stage else StageState.NEW
        if state == StageState.NEW:
            return loop.recruiter.name if loop.recruiter else "Recruiter"
        return loop.client_contact.name if loop.client_contact else "Client"

    def _build_input(
        self,
        suggestion: Suggestion,
        loop: Loop,
        stage: Stage | None,
        thread_messages: list[Message] | None,
    ) -> GenerateDraftInput:
        """Construct the drafter LLM input.

        The drafter is a "dumb tool" — the classifier provides the directive
        via action_data. We pass that along with recipient name, entities,
        and formatted thread messages for context.
        """
        thread_text = "No prior messages in this thread."
        if thread_messages:
            thread_text = format_thread_history(
                thread_messages, current_message_id=suggestion.gmail_message_id
            )

        # Read directive from action_data (typed contract), fall back to summary
        from api.classifier.models import DraftEmailData

        directive = suggestion.summary  # fallback
        if suggestion.action_data:
            try:
                draft_data = DraftEmailData.model_validate(suggestion.action_data)
                directive = draft_data.directive
            except Exception:
                logger.warning(
                    "could not parse action_data as DraftEmailData for suggestion %s, "
                    "falling back to summary",
                    suggestion.id,
                )

        to_emails, _ = resolve_recipients(loop, stage)
        is_external = any(
            not email.lower().endswith("@longridgepartners.com") for email in to_emails
        )

        return GenerateDraftInput(
            draft_directive=directive,
            recipient_name=self._resolve_recipient_name(loop, stage),
            candidate_name=loop.candidate.name if loop.candidate else "Candidate",
            coordinator_name=loop.coordinator.name if loop.coordinator else "Coordinator",
            extracted_entities=json.dumps(suggestion.extracted_entities),
            thread_messages=thread_text,
            is_external=is_external,
        )
