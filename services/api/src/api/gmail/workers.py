"""arq background workers for the Gmail push pipeline.

Handles push notifications, fallback polling, watch renewal, and
dedup cleanup. All processing converges on _process_history() which
is idempotent via the processed_messages table.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from arq import cron
from arq.connections import RedisSettings
from psycopg_pool import AsyncConnectionPool

from api.gmail.auth import TokenStore
from api.gmail.client import GmailClient
from api.gmail.exceptions import GmailNotFoundError, GmailScopeError
from api.gmail.hooks import (
    EmailEvent,
    LoggingHook,
    classify_direction,
    classify_message_type,
)

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "")
DEBOUNCE_TTL = 60  # seconds


async def startup(ctx: dict) -> None:
    """Initialize shared resources for all worker jobs."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()

    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
    gmail = GmailClient(token_store)

    ctx["db"] = pool
    ctx["token_store"] = token_store
    ctx["gmail"] = gmail

    # Initialize email hook: ClassifierHook if AI is configured, else LoggingHook
    classifier_enabled = os.environ.get("CLASSIFIER_ENABLED", "false").lower() == "true"
    if classifier_enabled:
        from api.ai import init_langfuse, init_llm_service
        from api.classifier.hook import ClassifierHook
        from api.classifier.suggestions import SuggestionService
        from api.scheduling.service import LoopService

        langfuse_client = init_langfuse()
        llm_service = init_llm_service()

        if langfuse_client and llm_service:
            loop_service = LoopService(db_pool=pool, gmail=gmail)
            suggestion_service = SuggestionService(db_pool=pool)
            ctx["hook"] = ClassifierHook(
                llm=llm_service,
                langfuse=langfuse_client,
                loop_service=loop_service,
                suggestion_service=suggestion_service,
                db_pool=pool,
            )
            ctx["langfuse"] = langfuse_client
            logger.info("Worker using ClassifierHook")
        else:
            ctx["hook"] = LoggingHook()
            logger.warning("CLASSIFIER_ENABLED=true but AI infra not available — using LoggingHook")
    else:
        ctx["hook"] = LoggingHook()

    logger.info("worker startup complete")


async def shutdown(ctx: dict) -> None:
    """Clean up shared resources."""
    langfuse_client = ctx.get("langfuse")
    if langfuse_client:
        langfuse_client.flush()
        langfuse_client.shutdown()
    pool = ctx.get("db")
    if pool:
        await pool.close()
    logger.info("worker shutdown complete")


async def process_gmail_push(ctx: dict, coordinator_email: str, history_id: str) -> None:
    """Process a Gmail push notification.

    Uses stored history_id as the cursor (more reliable than the push
    notification's history_id, which may be stale if multiple pushes
    arrive out of order).

    Debounce: uses a Redis lock per coordinator to prevent redundant
    processing when multiple push notifications arrive in rapid succession.
    """
    token_store: TokenStore = ctx["token_store"]

    # Debounce: skip if we already processed for this coordinator recently
    redis = ctx.get("redis")
    if redis:
        lock_key = f"push_lock:{coordinator_email}"
        locked = await redis.set(lock_key, "1", nx=True, ex=DEBOUNCE_TTL)
        if not locked:
            logger.debug("debounced push for %s", coordinator_email)
            return

    # Use our stored cursor, not the push notification's history_id
    stored_history_id = await token_store.get_history_id(coordinator_email)
    if not stored_history_id:
        # First push — establish baseline
        logger.info("first push for %s, establishing baseline", coordinator_email)
        await _establish_baseline(ctx, coordinator_email)
        return

    await _process_history(ctx, coordinator_email, stored_history_id)


async def poll_gmail_history(ctx: dict) -> None:
    """Fallback poll — process history for all watched coordinators.

    Runs every 60 seconds to catch any push notifications that were
    dropped, delayed, or missed during service restarts.
    """
    token_store: TokenStore = ctx["token_store"]
    emails = await token_store.get_all_watched_emails()

    for coordinator_email in emails:
        try:
            stored_history_id = await token_store.get_history_id(coordinator_email)
            if not stored_history_id:
                await _establish_baseline(ctx, coordinator_email)
                continue

            await _process_history(ctx, coordinator_email, stored_history_id)
        except GmailScopeError:
            logger.warning("scope error for %s — skipping until re-auth", coordinator_email)
        except Exception:
            logger.exception("poll error for %s", coordinator_email)


async def renew_gmail_watches(ctx: dict) -> None:
    """Re-register Pub/Sub watches before they expire (every 6 hours)."""
    if not PUBSUB_TOPIC:
        logger.debug("PUBSUB_TOPIC not configured — skipping watch renewal")
        return

    token_store: TokenStore = ctx["token_store"]
    gmail: GmailClient = ctx["gmail"]
    emails = await token_store.get_all_watched_emails()

    for coordinator_email in emails:
        try:
            result = await gmail.watch(coordinator_email, PUBSUB_TOPIC)
            expiry = datetime.fromtimestamp(int(result["expiration"]) / 1000, tz=UTC)
            await token_store.update_watch_state(
                coordinator_email,
                result["historyId"],
                expiry,
            )
            logger.info("renewed watch for %s, expires %s", coordinator_email, expiry)
        except GmailScopeError:
            logger.warning("scope error for %s — skipping watch renewal", coordinator_email)
        except Exception:
            logger.exception("watch renewal failed for %s", coordinator_email)


async def cleanup_processed_messages(ctx: dict) -> None:
    """Delete dedup records older than 30 days."""
    pool: AsyncConnectionPool = ctx["db"]
    async with pool.connection() as conn:
        result = await conn.execute(
            "DELETE FROM processed_messages WHERE processed_at < now() - INTERVAL '30 days'"
        )
        logger.info("cleaned up old processed messages: %s rows", result.rowcount)


# --- Internal helpers ---


async def _establish_baseline(ctx: dict, coordinator_email: str) -> None:
    """Set the initial history cursor for a coordinator without processing old emails."""
    gmail: GmailClient = ctx["gmail"]
    token_store: TokenStore = ctx["token_store"]

    try:
        profile = await gmail.get_profile(coordinator_email)
        history_id = profile["historyId"]
        await token_store.update_history_id(coordinator_email, str(history_id))
        logger.info("established baseline for %s at history_id=%s", coordinator_email, history_id)
    except GmailScopeError:
        raise
    except Exception:
        logger.exception("failed to establish baseline for %s", coordinator_email)


async def _process_history(ctx: dict, coordinator_email: str, start_history_id: str) -> None:
    """Core processing loop shared by push and poll paths.

    1. history.list(startHistoryId) → list of new message IDs
    2. For each message ID:
       a. Skip if already in processed_messages (idempotent)
       b. Mark as processed (at-most-once)
       c. Fetch full message
       d. Fetch thread for forward detection context
       e. Classify direction and type
       f. Build EmailEvent and fire hook
    3. Update stored last_history_id
    """
    gmail: GmailClient = ctx["gmail"]
    token_store: TokenStore = ctx["token_store"]
    pool: AsyncConnectionPool = ctx["db"]
    hook = ctx["hook"]

    try:
        history_response = await gmail.history_list(
            coordinator_email,
            start_history_id,
            history_types=["messageAdded"],
        )
    except GmailNotFoundError:
        # History ID expired (>30 days stale) — re-baseline
        logger.warning("history_id expired for %s, re-establishing baseline", coordinator_email)
        await _establish_baseline(ctx, coordinator_email)
        return

    # Extract new message IDs from history
    new_message_ids: list[str] = []
    for entry in history_response.get("history", []):
        for msg_added in entry.get("messagesAdded", []):
            msg_id = msg_added.get("message", {}).get("id")
            if msg_id:
                new_message_ids.append(msg_id)

    if not new_message_ids:
        # Advance cursor even if no new messages
        new_history_id = history_response.get("historyId")
        if new_history_id:
            await token_store.update_history_id(coordinator_email, str(new_history_id))
        return

    # Process each new message
    threads_cache: dict[str, list] = {}

    for msg_id in new_message_ids:
        try:
            # Dedup check
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT EXISTS("
                    "SELECT 1 FROM processed_messages "
                    "WHERE gmail_message_id = %(id)s)",
                    {"id": msg_id},
                )
                row = await cur.fetchone()
                if row and row[0]:
                    continue

            # Mark as processed BEFORE firing hook (at-most-once)
            async with pool.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO processed_messages (gmail_message_id, coordinator_email)
                    VALUES (%(id)s, %(email)s)
                    ON CONFLICT (gmail_message_id) DO NOTHING
                    """,
                    {"id": msg_id, "email": coordinator_email},
                )

            # Fetch full message
            message = await gmail.get_message(coordinator_email, msg_id)

            # Fetch thread for forward detection (cached per thread)
            thread_id = message.thread_id
            if thread_id not in threads_cache:
                thread = await gmail.get_thread(coordinator_email, thread_id)
                threads_cache[thread_id] = thread.messages
            thread_messages = threads_cache[thread_id]

            # Classify
            direction = classify_direction(message, coordinator_email)
            prior_messages = [
                m for m in thread_messages if m.id != message.id and m.date < message.date
            ]
            message_type, new_participants = classify_message_type(message, prior_messages)

            # Build and fire event
            event = EmailEvent(
                message=message,
                coordinator_email=coordinator_email,
                direction=direction,
                message_type=message_type,
                new_participants=new_participants,
            )
            # Attach thread messages for classifier context (not part of the model)
            event._thread_messages = thread_messages  # type: ignore[attr-defined]
            await hook.on_email(event)

        except Exception:
            logger.exception("failed to process message %s for %s", msg_id, coordinator_email)

    # Advance cursor
    new_history_id = history_response.get("historyId")
    if new_history_id:
        await token_store.update_history_id(coordinator_email, str(new_history_id))


class WorkerSettings:
    """arq worker configuration."""

    functions = [process_gmail_push]  # noqa: RUF012
    cron_jobs = [  # noqa: RUF012
        cron(poll_gmail_history, second=0),  # every 60s
        cron(renew_gmail_watches, hour={0, 6, 12, 18}, minute=0, second=0),
        cron(cleanup_processed_messages, hour=3, minute=0, second=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_jobs = 50
    job_timeout = 120
