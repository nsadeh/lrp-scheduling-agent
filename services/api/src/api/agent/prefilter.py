"""Scheduling relevance pre-filter.

Fast, cheap check to determine if an incoming email is worth processing
by the full agent. Deliberately conservative (high recall, moderate precision).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from api.agent.queries import queries

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg_pool import AsyncConnectionPool

    from api.gmail.models import Message

logger = logging.getLogger(__name__)

SCHEDULING_SIGNALS = [
    "interview",
    "schedule",
    "availability",
    "round 1",
    "round 2",
    "round 3",
    "meet",
    "candidate",
    "time slot",
    "reschedule",
    "cancel",
    "first round",
    "final round",
    "on-site",
    "onsite",
    "phone screen",
    "video call",
]


async def is_scheduling_relevant(
    message: Message,
    db_pool: AsyncConnectionPool,
    find_loop_by_thread: Callable,
) -> tuple[bool, str]:
    """Fast check: is this email likely about scheduling?

    Returns (is_relevant, reason) for observability.
    """
    # 1. Known thread — already linked to a loop
    loop = await find_loop_by_thread(message.thread_id)
    if loop is not None:
        return True, "known_thread"

    # 2. Known sender — email from a contact in our DB
    sender_email = message.from_.email if message.from_ else None
    if sender_email:
        async with db_pool.connection() as conn:
            result = await queries.has_known_contact(conn, email=sender_email)
            if result:
                return True, "known_sender"

    # 3. Keyword heuristic — check subject + snippet
    text = f"{message.subject or ''} {message.snippet or ''}".lower()
    for signal in SCHEDULING_SIGNALS:
        if signal in text:
            return True, f"keyword:{signal}"

    return False, "no_match"
