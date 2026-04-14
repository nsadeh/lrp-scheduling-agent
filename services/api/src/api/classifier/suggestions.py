"""Suggestion persistence service.

Handles creating, querying, resolving, and expiring agent suggestions.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from api.classifier.models import (
    Suggestion,
    SuggestionItem,
    SuggestionStatus,
)
from api.classifier.queries import queries
from api.ids import make_id

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


async def _collect(async_gen) -> list:
    return [row async for row in async_gen]


def _row_to_suggestion(row: tuple) -> Suggestion:
    entities = row[12]
    if isinstance(entities, str):
        entities = json.loads(entities)
    questions = row[13]
    if isinstance(questions, str):
        questions = json.loads(questions)

    return Suggestion(
        id=row[0],
        coordinator_email=row[1],
        gmail_message_id=row[2],
        gmail_thread_id=row[3],
        loop_id=row[4],
        stage_id=row[5],
        classification=row[6],
        action=row[7],
        auto_advance=row[8],
        confidence=row[9],
        summary=row[10],
        target_state=row[11],
        extracted_entities=entities,
        questions=questions,
        reasoning=row[14],
        status=row[15],
        resolved_at=row[16],
        resolved_by=row[17],
        created_at=row[18],
    )


class SuggestionService:
    """Manages agent suggestion lifecycle."""

    def __init__(self, db_pool: AsyncConnectionPool):
        self._pool = db_pool

    async def create_suggestion(
        self,
        *,
        coordinator_email: str,
        gmail_message_id: str,
        gmail_thread_id: str,
        item: SuggestionItem,
        reasoning: str | None = None,
        loop_id: str | None = None,
        stage_id: str | None = None,
    ) -> Suggestion:
        """Persist a classifier suggestion."""
        suggestion_id = make_id("sug")
        status = SuggestionStatus.AUTO_APPLIED if item.auto_advance else SuggestionStatus.PENDING

        async with self._pool.connection() as conn:
            row = await queries.create_suggestion(
                conn,
                id=suggestion_id,
                coordinator_email=coordinator_email,
                gmail_message_id=gmail_message_id,
                gmail_thread_id=gmail_thread_id,
                loop_id=loop_id or item.target_loop_id,
                stage_id=stage_id or item.target_stage_id,
                classification=item.classification.value,
                action=item.action.value,
                auto_advance=item.auto_advance,
                confidence=item.confidence,
                summary=item.summary,
                target_state=item.target_state.value if item.target_state else None,
                extracted_entities=json.dumps(item.extracted_entities),
                questions=json.dumps(item.questions),
                reasoning=reasoning,
                status=status.value,
            )
            return _row_to_suggestion(row)

    async def get_pending_for_coordinator(self, coordinator_email: str) -> list[Suggestion]:
        """Get all pending suggestions for a coordinator."""
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.get_pending_suggestions_for_coordinator(
                    conn, coordinator_email=coordinator_email
                )
            )
            return [_row_to_suggestion(r) for r in rows]

    async def get_for_thread(self, gmail_thread_id: str) -> list[Suggestion]:
        """Get all suggestions for a Gmail thread."""
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.get_suggestions_for_thread(conn, gmail_thread_id=gmail_thread_id)
            )
            return [_row_to_suggestion(r) for r in rows]

    async def get_pending_for_loop(self, loop_id: str) -> list[Suggestion]:
        """Get pending suggestions for a specific loop."""
        async with self._pool.connection() as conn:
            rows = await _collect(queries.get_pending_suggestions_for_loop(conn, loop_id=loop_id))
            return [_row_to_suggestion(r) for r in rows]

    async def resolve(
        self,
        suggestion_id: str,
        *,
        status: SuggestionStatus,
        resolved_by: str,
    ) -> None:
        """Mark a pending suggestion as accepted, rejected, etc."""
        async with self._pool.connection() as conn:
            await queries.resolve_suggestion(
                conn,
                id=suggestion_id,
                status=status.value,
                resolved_by=resolved_by,
            )

    async def supersede_pending_for_loop(self, loop_id: str, resolved_by: str) -> None:
        """Mark all pending suggestions for a loop as superseded."""
        async with self._pool.connection() as conn:
            await queries.supersede_pending_suggestions_for_loop(
                conn, loop_id=loop_id, resolved_by=resolved_by
            )

    async def expire_old_suggestions(self) -> None:
        """Expire pending suggestions older than 72 hours."""
        async with self._pool.connection() as conn:
            await queries.expire_old_suggestions(conn)
            logger.info("Expired old pending suggestions")
