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


def _is_internal_only(msg: Message) -> bool:
    all_addresses = [msg.from_.email, *(a.email for a in msg.to), *(a.email for a in msg.cc)]
    return all(addr.lower().endswith(f"@{INTERNAL_DOMAIN}") for addr in all_addresses)


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

        # 2. Internal-only messages
        if _is_internal_only(msg):
            logger.debug(
                "skipping internal-only email on thread %s (all participants @%s)",
                msg.thread_id,
                INTERNAL_DOMAIN,
            )
            return

        # 3. Check thread linkage
        linked_loops = await self._loops.find_loops_by_thread(msg.thread_id)

        if linked_loops:
            # Linked thread → Next Action Agent (inbound and outgoing)
            await self._agent.act(event, linked_loops, arq_pool=arq_pool)
        else:
            # Unlinked thread — only process inbound
            if event.direction == MessageDirection.OUTGOING:
                logger.debug(
                    "skipping outgoing email on unlinked thread %s",
                    msg.thread_id,
                )
                return

            await self._classifier.classify(event, arq_pool=arq_pool)
