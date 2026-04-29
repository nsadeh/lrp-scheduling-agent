"""OverviewService — fetches denormalized suggestion data for the sidebar UI.

Runs the JOIN query and groups results by loop_id for card rendering.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime  # noqa: TC003 — needed at runtime for type annotation
from typing import TYPE_CHECKING

from api.classifier.models import SuggestedAction, Suggestion
from api.classifier.queries import queries
from api.drafts.models import DraftStatus, EmailDraft
from api.overview.models import LoopSuggestionGroup, SuggestionView

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


async def _collect(async_gen) -> list:
    return [row async for row in async_gen]


def _row_to_suggestion_view(row: tuple) -> SuggestionView:
    """Convert a JOIN query row into a SuggestionView."""
    # Columns 0-19: suggestion fields (same order as get_suggestion)
    entities = row[12]
    if isinstance(entities, str):
        entities = json.loads(entities)
    questions = row[13]
    if isinstance(questions, str):
        questions = json.loads(questions)
    action_data = row[14]
    if isinstance(action_data, str):
        action_data = json.loads(action_data)

    suggestion = Suggestion(
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

    # Columns 20-22: loop context
    loop_title = row[20]
    candidate_name = row[21]
    client_company = row[22]

    # Columns 23-24: stage context
    stage_name = row[23]
    stage_state = row[24]

    # Columns 25-33: draft context (33 = pending_jit_data JSONB)
    draft = None
    draft_id = row[25]
    if draft_id is not None:
        draft_to = row[26]
        if isinstance(draft_to, str):
            draft_to = [draft_to]
        draft_cc = row[27]
        if isinstance(draft_cc, str):
            draft_cc = [draft_cc]
        if draft_cc is None:
            draft_cc = []
        pending_jit = row[33]
        if isinstance(pending_jit, str):
            pending_jit = json.loads(pending_jit)
        if pending_jit is None:
            pending_jit = {}
        draft = EmailDraft(
            id=draft_id,
            suggestion_id=suggestion.id,
            loop_id=suggestion.loop_id or "",
            stage_id=suggestion.stage_id or "",
            coordinator_email=suggestion.coordinator_email,
            to_emails=draft_to if draft_to else [],
            cc_emails=draft_cc if draft_cc else [],
            subject=row[28] or "",
            body=row[29] or "",
            status=DraftStatus(row[30]) if row[30] else DraftStatus.GENERATED,
            gmail_thread_id=row[31],
            is_forward=bool(row[32]) if row[32] is not None else False,
            pending_jit_data=pending_jit,
        )

    # Columns 34-39: known actor emails (always present in row, may be NULL)
    client_contact_name = row[34]
    client_contact_email = row[35]
    recruiter_name = row[36]
    recruiter_email = row[37]
    client_manager_name = row[38]
    client_manager_email = row[39]

    return SuggestionView(
        suggestion=suggestion,
        loop_title=loop_title,
        candidate_name=candidate_name,
        client_company=client_company,
        stage_name=stage_name,
        stage_state=stage_state,
        draft=draft,
        client_contact_name=client_contact_name,
        client_contact_email=client_contact_email,
        recruiter_name=recruiter_name,
        recruiter_email=recruiter_email,
        client_manager_name=client_manager_name,
        client_manager_email=client_manager_email,
    )


def _suggestion_sort_key(v: SuggestionView) -> tuple[int, datetime]:
    """Sort key: ADVANCE_STAGE last within each group, then by created_at."""
    is_advance = 1 if v.suggestion.action == SuggestedAction.ADVANCE_STAGE else 0
    return (is_advance, v.suggestion.created_at)


def group_by_loop(views: list[SuggestionView]) -> list[LoopSuggestionGroup]:
    """Group suggestion views by loop_id, sorted by oldest suggestion.

    Within each group, suggestions are ordered by creation date (oldest first),
    except ADVANCE_STAGE suggestions are always at the bottom — the coordinator
    should handle actionable items (drafts, links) before confirming state changes.
    """
    groups: dict[str | None, LoopSuggestionGroup] = {}
    for v in views:
        key = v.suggestion.loop_id
        if key not in groups:
            groups[key] = LoopSuggestionGroup(
                loop_id=key,
                loop_title=v.loop_title,
                candidate_name=v.candidate_name,
                client_company=v.client_company,
                suggestions=[],
                oldest_created_at=v.suggestion.created_at,
            )
        groups[key].suggestions.append(v)

    # Sort suggestions within each group
    for group in groups.values():
        group.suggestions.sort(key=_suggestion_sort_key)

    # Sort groups by oldest suggestion creation time
    return sorted(groups.values(), key=lambda g: g.oldest_created_at)


class OverviewService:
    def __init__(self, db_pool: AsyncConnectionPool):
        self._pool = db_pool

    async def get_overview_data(self, coordinator_email: str) -> list[LoopSuggestionGroup]:
        """Fetch all pending suggestions with context, grouped by loop."""
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.get_pending_suggestions_with_context(
                    conn, coordinator_email=coordinator_email
                )
            )
        views = [_row_to_suggestion_view(r) for r in rows]
        return group_by_loop(views)

    async def get_thread_overview_data(
        self, gmail_thread_id: str, coordinator_email: str
    ) -> list[LoopSuggestionGroup]:
        """Fetch pending suggestions for a specific thread, grouped by loop."""
        async with self._pool.connection() as conn:
            rows = await _collect(
                queries.get_pending_suggestions_for_thread_with_context(
                    conn,
                    gmail_thread_id=gmail_thread_id,
                    coordinator_email=coordinator_email,
                )
            )
        views = [_row_to_suggestion_view(r) for r in rows]
        return group_by_loop(views)
