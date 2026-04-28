"""arq background workers for the Gmail push pipeline.

Handles push notifications, fallback polling, watch renewal, and
dedup cleanup. All processing converges on _process_history() which
is idempotent via the processed_messages table.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import sentry_sdk
from arq import create_pool, cron
from arq.connections import RedisSettings
from psycopg_pool import AsyncConnectionPool

from api.gmail.auth import TokenStore
from api.gmail.client import GmailClient
from api.gmail.exceptions import GmailNotFoundError, GmailScopeError
from api.gmail.hooks import (
    EmailEvent,
    classify_direction,
    classify_message_type,
)
from api.observability import init_sentry

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "")
DEBOUNCE_TTL = 60  # seconds


async def startup(ctx: dict) -> None:
    """Initialize shared resources for all worker jobs.

    Crashes on startup if AI infrastructure (LangFuse, LLM provider keys) is not configured.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    init_sentry(service="worker")
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()

    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
    gmail = GmailClient(token_store)

    ctx["db"] = pool
    ctx["token_store"] = token_store
    ctx["gmail"] = gmail

    # AI infrastructure — required, crashes if not configured
    from api.ai import init_langfuse, init_llm_service
    from api.classifier.hook import ClassifierHook
    from api.classifier.sender_blacklist import load_blacklist
    from api.classifier.service import SuggestionService
    from api.drafts.service import DraftService
    from api.scheduling.service import LoopService

    langfuse = init_langfuse()
    llm = init_llm_service()

    loop_service = LoopService(db_pool=pool, gmail=gmail)
    draft_service = DraftService(
        db_pool=pool,
        loop_service=loop_service,
        llm=llm,
        langfuse=langfuse,
    )

    # Dedicated arq pool for the hook to enqueue follow-up reclassification
    # jobs (after CREATE_LOOP / LINK_THREAD auto-resolve). Separate from arq's
    # internal `ctx["redis"]` so it's lifecycle-managed by us.
    arq_pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    ctx["arq_pool"] = arq_pool

    ctx["hook"] = ClassifierHook(
        llm=llm,
        langfuse=langfuse,
        suggestion_service=SuggestionService(db_pool=pool),
        loop_service=loop_service,
        draft_service=draft_service,
        sender_blacklist=load_blacklist(),
        arq_pool=arq_pool,
    )
    logger.info("worker startup complete — ClassifierHook active")


async def shutdown(ctx: dict) -> None:
    """Clean up shared resources."""
    pool = ctx.get("db")
    if pool:
        await pool.close()
    arq_pool = ctx.get("arq_pool")
    if arq_pool is not None:
        await arq_pool.close()
    logger.info("worker shutdown complete")


async def on_job_start(ctx: dict) -> None:
    """Open a fresh Sentry scope tagged with arq job metadata.

    Arq reuses a single process for many jobs, so without per-job isolation the
    scope accumulates tags across jobs and errors get attributed to whichever
    job happened to run last.
    """
    scope = sentry_sdk.Scope.get_current_scope()
    scope.clear()
    scope.set_tag("service", "worker")
    scope.set_tag("arq.job_id", ctx.get("job_id"))
    scope.set_tag("arq.job_try", ctx.get("job_try"))
    scope.set_tag("arq.function", ctx.get("enqueue_job", {}).get("function", ""))


async def on_job_end(ctx: dict) -> None:
    """Flush buffered events before arq moves on to the next job."""
    client = sentry_sdk.get_client()
    if client is not None:
        client.flush(timeout=2.0)


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
        # No baseline yet (OAuth callback didn't set one, or legacy user).
        # Use the push notification's history_id so we don't skip any messages.
        logger.info("no baseline for %s, using push history_id=%s", coordinator_email, history_id)
        await token_store.update_history_id(coordinator_email, history_id)
        await _process_history(ctx, coordinator_email, history_id)
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
                thread_messages=thread_messages,
            )
            await hook.on_email(event)

        except Exception:
            logger.exception("failed to process message %s for %s", msg_id, coordinator_email)

    # Advance cursor
    new_history_id = history_response.get("historyId")
    if new_history_id:
        await token_store.update_history_id(coordinator_email, str(new_history_id))


async def reclassify_after_loop_creation(
    ctx: dict,
    coordinator_email: str,
    gmail_message_id: str | None,
    gmail_thread_id: str,
) -> None:
    """Re-run the classifier on a message after a loop is created for its thread.

    Enqueued by the addon when a coordinator creates a new loop. The thread is
    now linked, so the classifier will produce follow-up suggestions (DRAFT_EMAIL,
    ADVANCE_STAGE) that weren't possible before.
    """
    gmail: GmailClient = ctx["gmail"]
    hook = ctx["hook"]

    try:
        # Fetch the specific message, or fall back to latest on thread
        if gmail_message_id:
            message = await gmail.get_message(coordinator_email, gmail_message_id)
        else:
            thread = await gmail.get_thread(coordinator_email, gmail_thread_id)
            if not thread.messages:
                logger.warning("empty thread %s — skipping reclassification", gmail_thread_id)
                return
            message = thread.messages[-1]

        # Fetch full thread for context
        thread = await gmail.get_thread(coordinator_email, gmail_thread_id)
        thread_messages = thread.messages

        # Classify direction and type (same pattern as _process_history)
        direction = classify_direction(message, coordinator_email)
        prior_messages = [
            m for m in thread_messages if m.id != message.id and m.date < message.date
        ]
        message_type, new_participants = classify_message_type(message, prior_messages)

        event = EmailEvent(
            message=message,
            coordinator_email=coordinator_email,
            direction=direction,
            message_type=message_type,
            new_participants=new_participants,
            thread_messages=thread_messages,
        )
        await hook.on_email(event)
        logger.info(
            "reclassified message %s after loop creation (thread %s)",
            message.id,
            gmail_thread_id,
        )
    except Exception:
        logger.exception(
            "background reclassification failed for thread %s (coordinator %s)",
            gmail_thread_id,
            coordinator_email,
        )


class WorkerSettings:
    """arq worker configuration."""

    functions = [process_gmail_push, reclassify_after_loop_creation]  # noqa: RUF012
    cron_jobs = [  # noqa: RUF012
        cron(poll_gmail_history, second=0),  # every 60s
        cron(renew_gmail_watches, hour={0, 6, 12, 18}, minute=0, second=0),
        cron(cleanup_processed_messages, hour=3, minute=0, second=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    on_job_start = on_job_start
    on_job_end = on_job_end
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_jobs = 50
    job_timeout = 180
