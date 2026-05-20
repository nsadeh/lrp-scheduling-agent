"""Resolver outcome union and supporting dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class RecruiterCandidate:
    """One candidate recruiter from the Encore lookup."""

    email: str
    display_name: str
    last_activity: datetime
    genie_type: str


@dataclass(frozen=True)
class UniqueRecruiter:
    """Exactly one non-coordinator recruiter matched."""

    email: str
    display_name: str
    source: Literal["genie", "ana_initials"]


@dataclass(frozen=True)
class AmbiguousRecruiters:
    """2+ distinct non-coordinator recruiters matched (top 5, recency-sorted)."""

    candidates: list[RecruiterCandidate]


@dataclass(frozen=True)
class NoMatch:
    """No usable match. Reason is enum-like for log/metric grouping."""

    reason: Literal[
        "no_genie_rows",
        "no_non_coordinator",
        "ana_parse_failed",
        "ana_lookup_empty",
        "resolved_to_coordinator",
    ]


@dataclass(frozen=True)
class Skipped:
    """Resolver did not run. Coordinator-driven gate, not a query failure."""

    reason: Literal["coordinator_is_adam", "single_word_name"]


@dataclass(frozen=True)
class EncoreLookupError:
    """MS SQL query failed (connection, timeout, programming error)."""

    exception_type: str


ResolverOutcome = UniqueRecruiter | AmbiguousRecruiters | NoMatch | Skipped | EncoreLookupError
