"""Deterministic recruiter resolution from Encore Genie activity.

Public entry point: `resolve_recruiter(candidate_name, cutoff_date,
coordinator_email)`. Returns one of `UniqueRecruiter`, `AmbiguousRecruiters`,
`NoMatch`, `Skipped`, or `EncoreLookupError`. The CreateLoopResolver maps
each outcome to either a direct `set_recruiter` call or a synthesized
`UPDATE_ACTOR(role=recruiter)` suggestion attached to the new loop.

See rfcs/rfc-encore-recruiter-resolution.md §Resolution protocol for the
full state machine; the docstring on `resolve_recruiter` enumerates the
branches in code.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pymssql
import sentry_sdk

from api.encore import client
from api.encore.coordinators import ADAM_EMAIL, ANA_EMAIL, COORDINATOR_EMAILS
from api.encore.models import (
    AmbiguousRecruiters,
    EncoreLookupError,
    NoMatch,
    RecruiterCandidate,
    ResolverOutcome,
    Skipped,
    UniqueRecruiter,
)
from api.encore.queries import queries

logger = logging.getLogger(__name__)

_WINDOW_DAYS = 365
_AMBIGUOUS_TOP_N = 5
_INITIALS_RE = re.compile(r"^\s*([A-Z]{2,3})\b")


@dataclass(frozen=True)
class _Row:
    """Internal row shape from get_recent_candidate_activity."""

    user_guid: str
    time_entered: datetime
    genie_notes: str
    genie_type: str
    email: str
    display_name: str


async def resolve_recruiter(
    candidate_name: str,
    cutoff_date: datetime,
    coordinator_email: str,
) -> ResolverOutcome:
    """Resolve the recruiter for a candidate via Encore Genie history.

    Branches (in order):
    1. Coordinator is Adam → Skipped("coordinator_is_adam")
    2. Single-word candidate name → Skipped("single_word_name")
    3. Primary query fails → EncoreLookupError + sentry
    4. 0 raw rows → NoMatch("no_genie_rows")
    5. 0 non-coordinator rows + coordinator is Ana → Ana initials fallback
    6. 0 non-coordinator rows + coordinator is not Ana → NoMatch("no_non_coordinator")
    7. 1 distinct non-coordinator recruiter → UniqueRecruiter (coordinator-guard checked)
    8. 2+ distinct → AmbiguousRecruiters (top 5 by recency)
    """
    if coordinator_email == ADAM_EMAIL:
        logger.info("encore.resolve_recruiter skipped — coordinator is Adam")
        return Skipped(reason="coordinator_is_adam")

    parsed = _parse_name(candidate_name)
    if parsed is None:
        logger.info("encore.resolve_recruiter skipped — single-word name %r", candidate_name)
        return Skipped(reason="single_word_name")
    first_name, last_name = parsed

    cutoff = cutoff_date - timedelta(days=_WINDOW_DAYS)

    try:
        raw_rows = await asyncio.to_thread(
            client.with_cursor,
            lambda cur: list(
                queries.get_recent_candidate_activity(
                    cur,
                    last_name=last_name,
                    first_name_like=f"{first_name}%",
                    cutoff=cutoff,
                )
            ),
        )
    except (pymssql.OperationalError, pymssql.DatabaseError, TimeoutError) as exc:
        logger.exception("encore.resolve_recruiter MSSQL failure")
        sentry_sdk.capture_exception(exc)
        return EncoreLookupError(exception_type=type(exc).__name__)

    rows = [_to_row(r) for r in raw_rows if _row_has_required_fields(r)]
    logger.info(
        "encore.resolve_recruiter primary returned %d rows for candidate=%r coord=%s",
        len(rows),
        candidate_name,
        coordinator_email,
    )

    if not rows:
        return NoMatch(reason="no_genie_rows")

    non_coord_rows = [r for r in rows if r.email.lower() not in COORDINATOR_EMAILS]

    if not non_coord_rows:
        if coordinator_email == ANA_EMAIL:
            return await _ana_fallback(rows)
        return NoMatch(reason="no_non_coordinator")

    # Group by recruiter GUID, keep most-recent row per group, sort desc by recency.
    groups: dict[str, _Row] = {}
    for row in non_coord_rows:
        existing = groups.get(row.user_guid)
        if existing is None or row.time_entered > existing.time_entered:
            groups[row.user_guid] = row
    sorted_groups = sorted(groups.values(), key=lambda r: r.time_entered, reverse=True)

    if len(sorted_groups) == 1:
        winner = sorted_groups[0]
        return _guard_or_unique(winner.email, winner.display_name, source="genie")

    return AmbiguousRecruiters(
        candidates=[
            RecruiterCandidate(
                email=r.email,
                display_name=r.display_name,
                last_activity=r.time_entered,
                genie_type=r.genie_type,
            )
            for r in sorted_groups[:_AMBIGUOUS_TOP_N]
        ]
    )


async def _ana_fallback(rows: list[_Row]) -> ResolverOutcome:
    """Ana submits on others' behalf with notes like 'DC - submission ...'.

    Parse the leading 2-3 letter initials block, look up the matching
    tUser.Login, and return the resulting recruiter — guarded against
    resolving to a coordinator's own email.
    """
    ana_rows = [r for r in rows if r.email.lower() == ANA_EMAIL]
    if not ana_rows:
        return NoMatch(reason="ana_lookup_empty")

    latest = max(ana_rows, key=lambda r: r.time_entered)
    match = _INITIALS_RE.match(latest.genie_notes or "")
    initials = match.group(1) if match else None
    if not initials:
        logger.warning(
            "encore.ana_fallback notes format drift — could not parse initials from %r",
            (latest.genie_notes or "")[:80],
        )
        sentry_sdk.capture_message(
            "encore.ana_fallback notes regex failed",
            level="warning",
        )
        return NoMatch(reason="ana_parse_failed")

    try:
        lookup_rows = await asyncio.to_thread(
            client.with_cursor,
            lambda cur: list(queries.get_email_for_login(cur, initials=initials)),
        )
    except (pymssql.OperationalError, pymssql.DatabaseError, TimeoutError) as exc:
        logger.exception("encore.ana_fallback MSSQL failure on initials lookup")
        sentry_sdk.capture_exception(exc)
        return EncoreLookupError(exception_type=type(exc).__name__)

    if len(lookup_rows) != 1:
        return NoMatch(reason="ana_lookup_empty")

    row = lookup_rows[0]
    return _guard_or_unique(
        email=row["EmailAddress"],
        display_name=row.get("EmailDisplayName") or row["EmailAddress"],
        source="ana_initials",
    )


def _guard_or_unique(email: str, display_name: str, source: str) -> ResolverOutcome:
    """Last-line defense: never return a coordinator's email as a recruiter."""
    if email.lower() in COORDINATOR_EMAILS:
        logger.warning(
            "encore.resolver tail-guard rejected coordinator email %s (source=%s)",
            email,
            source,
        )
        sentry_sdk.capture_message(
            "encore.resolver resolved to coordinator email — guard fired",
            level="warning",
        )
        return NoMatch(reason="resolved_to_coordinator")
    return UniqueRecruiter(email=email, display_name=display_name, source=source)  # type: ignore[arg-type]


def _parse_name(full_name: str) -> tuple[str, str] | None:
    tokens = (full_name or "").strip().split()
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


def _row_has_required_fields(row: dict[str, Any]) -> bool:
    """Drop rows missing the join targets we depend on (LEFT JOIN can produce nulls)."""
    return bool(row.get("UserEnteredGUID") and row.get("EmailAddress") and row.get("TimeEntered"))


def _to_row(raw: dict[str, Any]) -> _Row:
    return _Row(
        user_guid=raw["UserEnteredGUID"],
        time_entered=raw["TimeEntered"],
        genie_notes=raw.get("GenieNotes") or "",
        genie_type=raw.get("GenieType") or "",
        email=raw["EmailAddress"],
        display_name=raw.get("EmailDisplayName") or raw["EmailAddress"],
    )
