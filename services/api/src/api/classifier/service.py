"""Suggestion persistence service — CRUD for agent_suggestions rows."""

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
    from datetime import datetime

    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


async def _collect(async_gen) -> list:
    return [row async for row in async_gen]


class SuggestionService:
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
        """Persist a single SuggestionItem from the classifier output."""
        sug_id = make_id("sug")
        # All suggestions require coordinator approval — no auto-applied status.
        status = SuggestionStatus.PENDING

        async with self._pool.connection() as conn, conn.transaction():
            row = await queries.create_suggestion(
                conn,
                id=sug_id,
                coordinator_email=coordinator_email,
                gmail_message_id=gmail_message_id,
                gmail_thread_id=gmail_thread_id,
                loop_id=item.target_loop_id or loop_id,
                stage_id=item.target_stage_id or stage_id,
                classification=item.classification,
                action=item.action,
                auto_advance=item.auto_advance,
                confidence=item.confidence,
                summary=item.summary,
                target_state=item.target_state,
                extracted_entities=json.dumps(item.extracted_entities),
                questions=json.dumps(item.questions),
                action_data=json.dumps(item.action_data),
                reasoning=reasoning,
                status=status,
            )
            return _row_to_suggestion(row)

    async def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        async with self._pool.connection() as conn:
            row = await queries.get_suggestion(conn, id=suggestion_id)
            return _row_to_suggestion(row) if row else None

    async def get_suggestions_for_thread(self, gmail_thread_id: str) -> list[Suggestion]:
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.get_suggestions_for_thread(conn, gmail_thread_id=gmail_thread_id)
            )
            return [_row_to_suggestion(r) for r in rows]

    async def get_pending_for_coordinator(self, coordinator_email: str) -> list[Suggestion]:
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.get_pending_suggestions_for_coordinator(
                    conn, coordinator_email=coordinator_email
                )
            )
            return [_row_to_suggestion(r) for r in rows]

    async def get_pending_for_loop(self, loop_id: str) -> list[Suggestion]:
        async with self._pool.connection() as conn:
            rows = await _collect(queries.get_pending_suggestions_for_loop(conn, loop_id=loop_id))
            return [_row_to_suggestion(r) for r in rows]

    async def resolve(self, suggestion_id: str, status: SuggestionStatus, resolved_by: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await queries.resolve_suggestion(
                conn, id=suggestion_id, status=status, resolved_by=resolved_by
            )

    async def supersede_pending_for_loop(self, loop_id: str, resolved_by: str) -> None:
        """Mark all pending suggestions for a loop as superseded."""
        async with self._pool.connection() as conn, conn.transaction():
            await queries.supersede_pending_suggestions_for_loop(
                conn, loop_id=loop_id, resolved_by=resolved_by
            )

    async def expire_old(self, cutoff: datetime) -> None:
        """Expire pending suggestions older than cutoff."""
        async with self._pool.connection() as conn, conn.transaction():
            await queries.expire_old_suggestions(conn, cutoff=cutoff)


def _row_to_suggestion(row: tuple) -> Suggestion:
    entities = row[12]
    if isinstance(entities, str):
        entities = json.loads(entities)
    questions = row[13]
    if isinstance(questions, str):
        questions = json.loads(questions)
    action_data = row[14]
    if isinstance(action_data, str):
        action_data = json.loads(action_data)

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
        action_data=action_data,
        reasoning=row[15],
        status=row[16],
        resolved_at=row[17],
        resolved_by=row[18],
        created_at=row[19],
    )
