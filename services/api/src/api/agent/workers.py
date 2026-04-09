"""arq background worker functions for Gmail sync and message processing.

Handles push notification processing, periodic history sync,
watch renewal, and the agent processing pipeline.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from api.agent.prefilter import is_scheduling_relevant
from api.agent.queries import queries

logger = logging.getLogger(__name__)

PUBSUB_TOPIC = os.environ.get("GMAIL_PUBSUB_TOPIC", "projects/lrp-scheduling/topics/gmail-push")


async def process_gmail_notification(ctx: dict, coordinator_email: str, history_id: str) -> None:
    """Process a Gmail push notification.

    1. Get stored history_id for this coordinator
    2. Fetch history since stored history_id (or fallback to the one from push)
    3. For each new message:
       a. Check if already processed (idempotent)
       b. Mark as processed
       c. Fetch message metadata (fast)
       d. Run pre-filter
       e. If relevant, enqueue full processing job
    4. Update stored history_id
    """
    token_store = ctx["token_store"]
    gmail = ctx["gmail"]
    db = ctx["db"]
    redis = ctx["redis"]

    stored_history_id = await token_store.get_history_id(coordinator_email)
    start_history_id = stored_history_id or history_id

    logger.info(
        "Processing push for %s: stored=%s push=%s using=%s",
        coordinator_email,
        stored_history_id,
        history_id,
        start_history_id,
    )

    await _process_history(ctx, coordinator_email, start_history_id, gmail, db, token_store, redis)


async def process_relevant_message(
    ctx: dict, coordinator_email: str, message_id: str, thread_id: str
) -> None:
    """Process a single scheduling-relevant email through the agent pipeline.

    This is where the full agent runs:
    1. Fetch full message and thread
    2. Find matching loop (if any)
    3. Acquire debounce lock (one agent run per thread per 60s)
    4. Build agent context
    5. Run agent (classification + optional draft)
    6. Persist suggestion

    The agent engine is built separately — this wires together fetching,
    context assembly, and suggestion persistence.
    """
    gmail = ctx["gmail"]
    redis = ctx["redis"]
    scheduling = ctx["scheduling"]

    # Debounce: at most one agent run per thread per 60 seconds
    lock_acquired = await redis.set(f"debounce:{thread_id}", "1", ex=60, nx=True)
    if not lock_acquired:
        logger.info(
            "Debounce lock held for thread %s, skipping message %s",
            thread_id,
            message_id,
        )
        return

    logger.info(
        "Processing relevant message %s in thread %s for %s",
        message_id,
        thread_id,
        coordinator_email,
    )

    try:
        # 1. Fetch full message and thread
        message = await gmail.get_message(coordinator_email, message_id)
        thread = await gmail.get_thread(coordinator_email, thread_id)

        # 2. Find matching loop (if any)
        loop = await scheduling.find_loop_by_thread(thread_id)

        # 3. Build agent context
        _agent_context = {
            "coordinator_email": coordinator_email,
            "message": message,
            "thread": thread,
            "loop": loop,
        }

        # TODO: Run agent engine (classification + optional draft)
        # This will be implemented as part of the agent engine phase.
        # The agent engine will:
        #   - Classify the email (new request, availability response, confirmation, etc.)
        #   - Determine suggested action (draft reply, update stage, create loop, etc.)
        #   - Generate draft email if applicable
        #   - Return a structured suggestion
        logger.info(
            "Agent engine not yet implemented — skipping classification for message %s",
            message_id,
        )

        # TODO: Persist suggestion via queries.create_suggestion()
        # Will be wired once the agent engine returns structured output.

    except Exception:
        logger.exception("Error processing message %s for %s", message_id, coordinator_email)
        raise


async def renew_gmail_watches(ctx: dict) -> None:
    """Renew Gmail Pub/Sub watches for all coordinators.

    Runs every 6 hours. For each coordinator with a stored token:
    1. Call gmail.watch() to renew
    2. Update watch_expiry and history_id in token store
    """
    token_store = ctx["token_store"]
    gmail = ctx["gmail"]

    coordinators = await token_store.get_all_coordinators_with_tokens()
    logger.info("Renewing Gmail watches for %d coordinators", len(coordinators))

    for email in coordinators:
        try:
            result = await gmail.watch(email, PUBSUB_TOPIC)
            watch_history_id = str(result.get("historyId", ""))
            expiration_ms = int(result.get("expiration", 0))
            watch_expiry = datetime.fromtimestamp(expiration_ms / 1000, tz=UTC)

            await token_store.update_watch_state(email, watch_history_id, watch_expiry)
            logger.info("Renewed watch for %s, expires=%s", email, watch_expiry.isoformat())
        except Exception:
            logger.exception("Failed to renew watch for %s", email)


async def sync_gmail_history(ctx: dict) -> None:
    """Pull-based fallback sync for all coordinators.

    Runs every 5 minutes. For each coordinator:
    1. Fetch history since stored history_id
    2. Process any messages that weren't caught by push
    Same logic as process_gmail_notification but triggered by timer.
    """
    token_store = ctx["token_store"]
    gmail = ctx["gmail"]
    db = ctx["db"]
    redis = ctx["redis"]

    coordinators = await token_store.get_all_coordinators_with_tokens()
    logger.info("Sync history for %d coordinators", len(coordinators))

    for email in coordinators:
        try:
            stored_history_id = await token_store.get_history_id(email)
            if not stored_history_id:
                logger.debug("No stored history_id for %s, skipping sync", email)
                continue

            await _process_history(ctx, email, stored_history_id, gmail, db, token_store, redis)
        except Exception:
            logger.exception("Failed to sync history for %s", email)


async def cleanup_old_processed_messages(ctx: dict) -> None:
    """Clean up processed message records older than 30 days."""
    db = ctx["db"]
    async with db.connection() as conn:
        await queries.cleanup_old_processed_messages(conn)
    logger.info("Cleaned up old processed message records")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _process_history(
    ctx: dict,
    coordinator_email: str,
    start_history_id: str,
    gmail,
    db,
    token_store,
    redis,
) -> None:
    """Shared logic for processing Gmail history (push + pull)."""
    scheduling = ctx["scheduling"]

    try:
        result = await gmail.history_list(
            coordinator_email,
            start_history_id,
            history_types=["messageAdded"],
        )
    except Exception:
        logger.exception(
            "Failed to fetch history for %s from %s",
            coordinator_email,
            start_history_id,
        )
        return

    history_records = result.get("history", [])
    new_history_id = result.get("historyId", start_history_id)

    # Collect all new message IDs
    message_ids: list[str] = []
    for record in history_records:
        message_ids.extend(record.messages_added)

    logger.info(
        "History for %s: %d records, %d new messages, historyId=%s",
        coordinator_email,
        len(history_records),
        len(message_ids),
        new_history_id,
    )

    for msg_id in message_ids:
        try:
            # Idempotent: skip already-processed messages
            async with db.connection() as conn:
                already_processed = await queries.is_message_processed(
                    conn, gmail_message_id=msg_id
                )
            if already_processed:
                logger.debug("Message %s already processed, skipping", msg_id)
                continue

            # Mark as processed before doing work (at-most-once per message)
            async with db.connection() as conn:
                await queries.mark_message_processed(
                    conn, gmail_message_id=msg_id, coordinator_email=coordinator_email
                )

            # Fetch metadata (fast, headers only)
            metadata = await gmail.get_message_metadata(coordinator_email, msg_id)
            thread_id = metadata["threadId"]

            # Build a lightweight Message for the pre-filter
            # get_message_metadata returns headers dict; we need a full Message
            # for the pre-filter. Fetch full message for pre-filtering.
            message = await gmail.get_message(coordinator_email, msg_id)

            # Run pre-filter
            relevant, reason = await is_scheduling_relevant(
                message, db, scheduling.find_loop_by_thread
            )

            if relevant:
                logger.info(
                    "Message %s is relevant (%s), enqueueing processing",
                    msg_id,
                    reason,
                )
                await redis.enqueue_job(
                    "process_relevant_message",
                    coordinator_email,
                    msg_id,
                    thread_id,
                )
            else:
                logger.debug("Message %s not relevant (%s)", msg_id, reason)

        except Exception:
            logger.exception("Error processing message %s for %s", msg_id, coordinator_email)

    # Update stored history_id to latest
    await token_store.update_history_id(coordinator_email, new_history_id)
