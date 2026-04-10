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


# Column names as returned by RETURNING / SELECT in agent.sql.
# Used to map positional tuples to named fields.
_SUGGESTION_COLUMNS = (
    "id",
    "loop_id",
    "stage_id",
    "gmail_message_id",
    "gmail_thread_id",
    "classification",
    "suggested_action",
    "questions",
    "reasoning",
    "confidence",
    "prefilled_data",
    "status",
    "coordinator_feedback",
    "created_at",
    "resolved_at",
    "coordinator_email",
)

_DRAFT_COLUMNS = (
    "id",
    "suggestion_id",
    "draft_to",
    "draft_subject",
    "draft_body",
    "in_reply_to",
    "created_at",
)


def _row_to_dict(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    """Convert a positional tuple row to a dict using known column names."""
    if isinstance(row, dict):
        return row
    return {col: row[i] for i, col in enumerate(columns) if i < len(row)}


class Suggestion:
    """Lightweight suggestion data from the database."""

    def __init__(self, row: Any):
        d = _row_to_dict(row, _SUGGESTION_COLUMNS)
        self.id: str = d["id"]
        self.loop_id: str | None = d.get("loop_id")
        self.stage_id: str | None = d.get("stage_id")
        self.gmail_message_id: str = d["gmail_message_id"]
        self.gmail_thread_id: str = d["gmail_thread_id"]
        self.classification: str = d["classification"]
        self.suggested_action: str = d["suggested_action"]
        self.questions: list[str] = d.get("questions") or []
        self.reasoning: str | None = d.get("reasoning")
        self.confidence: float = d["confidence"]
        self.prefilled_data: dict | None = d.get("prefilled_data")
        self.status: str = d["status"]
        self.coordinator_feedback: str | None = d.get("coordinator_feedback")
        self.created_at = d.get("created_at")
        self.resolved_at = d.get("resolved_at")
        self.coordinator_email: str = d.get("coordinator_email", "")


class SuggestionDraft:
    """Lightweight draft data from the database."""

    def __init__(self, row: Any):
        d = _row_to_dict(row, _DRAFT_COLUMNS)
        self.id: str = d["id"]
        self.suggestion_id: str = d["suggestion_id"]
        self.draft_to: list[str] = d.get("draft_to") or []
        self.draft_subject: str = d["draft_subject"]
        self.draft_body: str = d["draft_body"]
        self.in_reply_to: str | None = d.get("in_reply_to")
        self.created_at = d.get("created_at")


class AgentService:
    """Manages agent suggestions and their lifecycle."""

    def __init__(self, db_pool: AsyncConnectionPool):
        self._pool = db_pool

    async def create_suggestion(
        self,
        *,
        coordinator_email: str,
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
                coordinator_email=coordinator_email,
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

    async def get_latest_for_thread(
        self, gmail_thread_id: str, coordinator_email: str
    ) -> Suggestion | None:
        """Get the most recent suggestion for a Gmail thread, scoped to a coordinator."""
        async with self._pool.connection() as conn:
            row = await queries.get_latest_suggestion_for_thread(
                conn,
                gmail_thread_id=gmail_thread_id,
                coordinator_email=coordinator_email,
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
