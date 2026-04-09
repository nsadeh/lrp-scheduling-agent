"""Agent suggestion service.

Persists agent suggestions and drafts, resolves suggestions,
and queries pending suggestions for the sidebar UI.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from api.agent.queries import queries
from api.ids import make_id

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


class Suggestion:
    """Lightweight suggestion data from the database."""

    def __init__(self, row: Any):
        self.id: str = row[0]
        self.loop_id: str | None = row[1]
        self.stage_id: str | None = row[2]
        self.gmail_message_id: str = row[3]
        self.gmail_thread_id: str = row[4]
        self.classification: str = row[5]
        self.suggested_action: str = row[6]
        self.questions: list[str] = row[7] or []
        self.reasoning: str | None = row[8]
        self.confidence: float = row[9]
        self.prefilled_data: dict | None = row[10]
        self.status: str = row[11]
        self.coordinator_feedback: str | None = row[12]
        self.created_at = row[13]
        self.resolved_at = row[14]


class SuggestionDraft:
    """Lightweight draft data from the database."""

    def __init__(self, row: Any):
        self.id: str = row[0]
        self.suggestion_id: str = row[1]
        self.draft_to: list[str] = row[2]
        self.draft_subject: str = row[3]
        self.draft_body: str = row[4]
        self.in_reply_to: str | None = row[5]
        self.created_at = row[6]


class AgentService:
    """Manages agent suggestions and their lifecycle."""

    def __init__(self, db_pool: AsyncConnectionPool):
        self._pool = db_pool

    async def create_suggestion(
        self,
        *,
        loop_id: str | None,
        stage_id: str | None,
        gmail_message_id: str,
        gmail_thread_id: str,
        classification: str,
        suggested_action: str,
        confidence: float,
        reasoning: str | None = None,
        questions: list[str] | None = None,
        prefilled_data: dict | None = None,
    ) -> Suggestion:
        """Create a new agent suggestion."""
        suggestion_id = make_id("asg")
        async with self._pool.connection() as conn:
            row = await queries.create_suggestion(
                conn,
                id=suggestion_id,
                loop_id=loop_id,
                stage_id=stage_id,
                gmail_message_id=gmail_message_id,
                gmail_thread_id=gmail_thread_id,
                classification=classification,
                suggested_action=suggested_action,
                questions=questions or [],
                reasoning=reasoning,
                confidence=confidence,
                prefilled_data=json.dumps(prefilled_data) if prefilled_data else None,
            )
            return Suggestion(row)

    async def create_draft(
        self,
        *,
        suggestion_id: str,
        draft_to: list[str],
        draft_subject: str,
        draft_body: str,
        in_reply_to: str | None = None,
    ) -> SuggestionDraft:
        """Create a draft email for a suggestion."""
        draft_id = make_id("sgd")
        async with self._pool.connection() as conn:
            row = await queries.create_suggestion_draft(
                conn,
                id=draft_id,
                suggestion_id=suggestion_id,
                draft_to=draft_to,
                draft_subject=draft_subject,
                draft_body=draft_body,
                in_reply_to=in_reply_to,
            )
            return SuggestionDraft(row)

    async def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        """Get a suggestion by ID."""
        async with self._pool.connection() as conn:
            row = await queries.get_suggestion(conn, id=suggestion_id)
            return Suggestion(row) if row else None

    async def get_draft_for_suggestion(self, suggestion_id: str) -> SuggestionDraft | None:
        """Get the draft associated with a suggestion."""
        async with self._pool.connection() as conn:
            row = await queries.get_draft_for_suggestion(conn, suggestion_id=suggestion_id)
            return SuggestionDraft(row) if row else None

    async def get_latest_for_thread(self, gmail_thread_id: str) -> Suggestion | None:
        """Get the most recent suggestion for a Gmail thread."""
        async with self._pool.connection() as conn:
            row = await queries.get_latest_suggestion_for_thread(
                conn, gmail_thread_id=gmail_thread_id
            )
            return Suggestion(row) if row else None

    async def get_latest_for_loop(self, loop_id: str) -> Suggestion | None:
        """Get the most recent suggestion for a loop."""
        async with self._pool.connection() as conn:
            row = await queries.get_latest_suggestion_for_loop(conn, loop_id=loop_id)
            return Suggestion(row) if row else None

    async def get_pending_for_coordinator(self, coordinator_email: str) -> list[Suggestion]:
        """Get all pending suggestions for a coordinator."""
        results = []
        async with self._pool.connection() as conn:
            async for row in queries.get_pending_suggestions_for_coordinator(
                conn, coordinator_email=coordinator_email
            ):
                results.append(Suggestion(row))
        return results

    async def resolve_suggestion(
        self,
        suggestion_id: str,
        *,
        status: str,
        coordinator_feedback: str | None = None,
    ) -> None:
        """Mark a suggestion as accepted/edited/rejected."""
        async with self._pool.connection() as conn:
            await queries.resolve_suggestion(
                conn,
                id=suggestion_id,
                status=status,
                coordinator_feedback=coordinator_feedback,
            )
