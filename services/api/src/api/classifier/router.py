"""EmailRouter — routes incoming emails to the LoopClassifier or NextActionAgent.

Replaces ClassifierHook.on_email() as the single entry point for the
email processing pipeline. Pre-filters (blacklist, internal-only) run
before any DB queries to minimize unnecessary Postgres round trips.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from api.classifier.sender_blacklist import SenderBlacklist
from api.gmail.hooks import MessageDirection

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from api.classifier.loop_classifier import LoopClassifier
    from api.classifier.next_action_agent import NextActionAgent
    from api.gmail.hooks import EmailEvent
    from api.gmail.models import Message
    from api.scheduling.service import LoopService

logger = logging.getLogger(__name__)

INTERNAL_DOMAIN = "longridgepartners.com"


def _message_addresses(msg: Message) -> list[str]:
    return [msg.from_.email, *(a.email for a in msg.to), *(a.email for a in msg.cc)]


def _is_internal_only(msg: Message, thread_messages: list[Message] | None = None) -> bool:
    """True iff every participant across the thread is at the internal domain.

    We check the *thread* — not just the trigger message — because of cases like:
      1. Client requests an interview (external sender).
      2. Coordinator forwards the request to the recruiter (internal-only).
      3. Recruiter replies with avails (internal-only trigger message).
    The recruiter's reply on its own looks internal-only, but the thread is
    clearly scheduling-related because the client is upstream. Looking at all
    thread participants keeps step 3 eligible for loop creation.
    """
    messages = thread_messages if thread_messages else [msg]
    for m in messages:
        for addr in _message_addresses(m):
            if not addr.lower().endswith(f"@{INTERNAL_DOMAIN}"):
                return False
    return True


class EmailRouter:
    """Routes emails to the correct handler based on thread linkage."""

    def __init__(
        self,
        *,
        loop_classifier: LoopClassifier,
        next_action_agent: NextActionAgent,
        loop_service: LoopService,
        sender_blacklist: SenderBlacklist | None = None,
    ):
        self._classifier = loop_classifier
        self._agent = next_action_agent
        self._loops = loop_service
        self._sender_blacklist = sender_blacklist or SenderBlacklist.empty()

    async def on_email(
        self,
        event: EmailEvent,
        *,
        arq_pool: ArqRedis | None = None,
        generation_token: str | None = None,
    ) -> None:
        msg = event.message

        # 1. Sender blacklist — no DB query needed
        if self._sender_blacklist.is_blocked(msg.from_.email):
            logger.debug(
                "skipping blacklisted sender %s on thread %s",
                msg.from_.email,
                msg.thread_id,
            )
            return

        # 2. Check thread linkage. We need this BEFORE the internal-only filter
        # because internal LRP-to-LRP traffic (recruiter sending avails to the
        # coordinator, internal updates, etc.) is the substance of the work on
        # a linked thread — dropping it would blind the agent to the very
        # messages that drive a loop forward.
        linked_loops = await self._loops.find_loops_by_thread(msg.thread_id)

        if linked_loops:
            # Linked thread → Next Action Agent (inbound and outgoing).
            # Internal-only messages are kept here: recruiter→coordinator on a
            # live loop is signal, not noise.
            await self._agent.act(
                event,
                linked_loops,
                arq_pool=arq_pool,
                generation_token=generation_token,
                generation_thread_id=msg.thread_id if generation_token else None,
            )
            return

        # 3. Unlinked thread. Now the internal-only filter applies — random
        # LRP-to-LRP chatter shouldn't try to spawn a new scheduling loop.
        # Check the *thread* participants, not just the trigger message, so
        # internal-only replies on a thread originally started by an external
        # party (e.g., coordinator forwarded a client request to a recruiter,
        # recruiter is now replying) still reach the classifier.
        if _is_internal_only(msg, event.thread_messages):
            logger.debug(
                "skipping internal-only thread %s (all participants across the thread are @%s)",
                msg.thread_id,
                INTERNAL_DOMAIN,
            )
            return

        # Only inbound messages on unlinked threads can create a new loop.
        if event.direction == MessageDirection.OUTGOING:
            logger.debug(
                "skipping outgoing email on unlinked thread %s",
                msg.thread_id,
            )
            return

        await self._classifier.classify(event, arq_pool=arq_pool)
