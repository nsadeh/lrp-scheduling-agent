"""Encore (Cluein) MS SQL access layer.

Read-only lookups against LRP's ATS database. Used by the CreateLoopResolver
to auto-set a loop's recruiter when the candidate has a unique recent
non-coordinator updater in Encore. See rfcs/rfc-encore-recruiter-resolution.md.

`resolve_recruiter` is the public entry point and lives in `resolver.py`.
"""

from api.encore.models import (
    AmbiguousRecruiters,
    EncoreLookupError,
    NoMatch,
    RecruiterCandidate,
    ResolverOutcome,
    Skipped,
    UniqueRecruiter,
)

__all__ = [
    "AmbiguousRecruiters",
    "EncoreLookupError",
    "NoMatch",
    "RecruiterCandidate",
    "ResolverOutcome",
    "Skipped",
    "UniqueRecruiter",
]
