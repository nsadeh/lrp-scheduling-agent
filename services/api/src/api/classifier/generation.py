"""Per-thread generation tokens for cancelling superseded agent runs.

Three workers can fire NextActionAgent.act() on the same Gmail thread
concurrently (run_next_action_agent, process_coordinator_response,
process_rejection_response). Without coordination they race, don't see each
other's writes, and produce duplicate suggestions.

This module adds cooperative cancellation: each worker handler calls
``claim_generation(redis, thread_id)`` on entry, passes the returned token
and thread_id into ``NextActionAgent.act()``, and the agent checks
``is_current_generation()`` at two points (before the LLM call and before
persisting suggestions). If a newer worker overwrites the token in between,
the older run logs and aborts without writing.

The pattern mirrors the existing ``push_lock`` debounce in
``api.gmail.workers``: Redis SET with a TTL as the coordination primitive,
last-writer-wins, fail-open when Redis is unavailable.

Cooperative cancellation only — we don't kill in-flight LLM calls or
interrupt the event loop. Worst case is one wasted LLM round-trip.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from arq.connections import ArqRedis

logger = logging.getLogger(__name__)

_GENERATION_TTL_SECONDS = 300  # 5 minutes — generous bound on a single agent run.
_KEY_FMT = "agent_gen:{thread_id}"


async def claim_generation(redis: ArqRedis | None, thread_id: str) -> str | None:
    """Stamp this thread with a fresh generation token. Returns the token.

    Returns None when ``redis`` is None — caller treats that as
    "no cancellation available," and ``is_current_generation`` will fail
    open. This keeps unit tests + degraded-Redis paths from blocking
    legitimate agent work.

    Last-writer-wins on SET. Two workers racing each other will both claim
    successfully but with different tokens; the earlier one aborts at its
    next checkpoint when it sees the newer token.
    """
    if redis is None:
        return None
    token = uuid4().hex
    await redis.set(_KEY_FMT.format(thread_id=thread_id), token, ex=_GENERATION_TTL_SECONDS)
    return token


async def is_current_generation(
    redis: ArqRedis | None, thread_id: str | None, token: str | None
) -> bool:
    """True iff the stored token matches ours.

    Returns True (continue) when any of redis/thread_id/token is None — this
    is the fail-open path. Used to gate the agent's LLM call and persist loop
    behind an "are we still the current generation?" check.

    Returns False when the stored token doesn't match OR is missing (TTL
    expired or someone explicitly cleared it). Treating "missing" as
    superseded is intentional: an LLM call that takes longer than the TTL
    is an outlier and is safer to drop than to write potentially-stale
    suggestions over a newer generation's work.
    """
    if redis is None or thread_id is None or token is None:
        return True
    stored = await redis.get(_KEY_FMT.format(thread_id=thread_id))
    if stored is None:
        return False
    if isinstance(stored, bytes):
        stored = stored.decode("utf-8")
    return stored == token
