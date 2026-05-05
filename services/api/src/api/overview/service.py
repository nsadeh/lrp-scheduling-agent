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
    """Convert a JOIN query row into a SuggestionView.

    Column layout (from suggestions.sql get_pending_suggestions_with_context):
      0-14:  suggestion (15 cols, see _row_to_suggestion in classifier/service.py)
      15-18: loop context (loop_title, loop_state, candidate_name, client_company)
      19-27: draft context (id, to_emails, cc_emails, subject, body, status,
             gmail_thread_id, is_forward, pending_jit_data)
      28-33: known actor emails (client_contact_name/email, recruiter_name/email,
             client_manager_name/email)
    """
    action_data = row[9]
    if isinstance(action_data, str):
        action_data = json.loads(action_data)

    suggestion = Suggestion(
        id=row[0],
        coordinator_email=row[1],
        gmail_message_id=row[2],
        gmail_thread_id=row[3],
        loop_id=row[4],
        classification=row[5],
        action=row[6],
        confidence=row[7],
        summary=row[8],
        action_data=action_data,
        reasoning=row[10],
        status=row[11],
        resolved_at=row[12],
        resolved_by=row[13],
        created_at=row[14],
    )

    loop_title = row[15]
    loop_state = row[16]
    candidate_name = row[17]
    client_company = row[18]

    draft = None
    draft_id = row[19]
    if draft_id is not None:
        draft_to = row[20]
        if isinstance(draft_to, str):
            draft_to = [draft_to]
        draft_cc = row[21]
        if isinstance(draft_cc, str):
            draft_cc = [draft_cc]
        if draft_cc is None:
            draft_cc = []
        pending_jit = row[27]
        if isinstance(pending_jit, str):
            pending_jit = json.loads(pending_jit)
        if pending_jit is None:
            pending_jit = {}
        draft = EmailDraft(
            id=draft_id,
            suggestion_id=suggestion.id,
            loop_id=suggestion.loop_id or "",
            coordinator_email=suggestion.coordinator_email,
            to_emails=draft_to if draft_to else [],
            cc_emails=draft_cc if draft_cc else [],
            subject=row[22] or "",
            body=row[23] or "",
            status=DraftStatus(row[24]) if row[24] else DraftStatus.GENERATED,
            gmail_thread_id=row[25],
            is_forward=bool(row[26]) if row[26] is not None else False,
            pending_jit_data=pending_jit,
        )

    client_contact_name = row[28]
    client_contact_email = row[29]
    recruiter_name = row[30]
    recruiter_email = row[31]
    client_manager_name = row[32]
    client_manager_email = row[33]

    return SuggestionView(
        suggestion=suggestion,
        loop_title=loop_title,
        loop_state=loop_state,
        candidate_name=candidate_name,
        client_company=client_company,
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
    """Group suggestion views by loop_id, sorted by oldest suggestion."""
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

    for group in groups.values():
        group.suggestions.sort(key=_suggestion_sort_key)

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
