"""arq background worker functions for Gmail sync and message processing.

Handles push notification processing, periodic history sync,
watch renewal, and the agent processing pipeline.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime

from api.agent.engine import run_agent
from api.agent.guardrails import validate_action
from api.agent.models import AgentContext
from api.agent.prefilter import is_scheduling_relevant
from api.agent.queries import queries

logger = logging.getLogger(__name__)

PUBSUB_TOPIC = os.environ.get(
    "GMAIL_PUBSUB_TOPIC", "projects/ai-agents-dev-492713/topics/gmail-push"
)


def _get_langfuse():
    """Return the Langfuse singleton, or None if unavailable."""
    try:
        from langfuse import get_client

        return get_client()
    except Exception:
        return None


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

    Creates a top-level Langfuse trace with child spans for each step.
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

    # Set up Langfuse trace for the full pipeline
    langfuse = _get_langfuse()
    trace = None
    if langfuse:
        try:
            trace = langfuse.trace(
                name="process-message",
                user_id=coordinator_email,
                session_id=thread_id,
                tags=["agent", "scheduling"],
                input={
                    "coordinator_email": coordinator_email,
                    "message_id": message_id,
                    "thread_id": thread_id,
                },
            )
        except Exception:
            logger.debug("Failed to create Langfuse trace", exc_info=True)

    try:
        # 1. Fetch full message and thread
        fetch_span = trace.span(name="fetch-context") if trace else None

        message = await gmail.get_message(coordinator_email, message_id)
        thread = await gmail.get_thread(coordinator_email, thread_id)

        # 2. Find matching loop (if any)
        loop = await scheduling.find_loop_by_thread(thread_id)

        # 3. Check if agent engine is available
        classifier = ctx.get("classifier")
        drafter = ctx.get("drafter")
        agent_service = ctx.get("agent_service")

        if not classifier or not drafter or not agent_service:
            logger.warning("Agent engine not available — skipping for message %s", message_id)
            return

        # 4. Build agent context — auto-create coordinator if needed.
        # Anyone who completed the OAuth flow is a coordinator by definition.
        coordinator = await scheduling.get_coordinator_by_email(coordinator_email)
        if not coordinator:
            fallback_name = coordinator_email.split("@")[0].replace(".", " ").title()
            coordinator = await scheduling.get_or_create_coordinator(
                name=fallback_name, email=coordinator_email
            )

        events = []
        recruiter = None
        client_contact = None
        candidate = None
        active_stage = None

        if loop:
            events = await scheduling.get_events(loop.id)
            if loop.recruiter:
                recruiter = loop.recruiter
            if loop.client_contact:
                client_contact = loop.client_contact
            if loop.candidate:
                candidate = loop.candidate
            if loop.most_urgent_stage:
                active_stage = loop.most_urgent_stage

        if fetch_span:
            with contextlib.suppress(Exception):
                fetch_span.end(
                    output={
                        "has_loop": loop is not None,
                        "loop_id": loop.id if loop else None,
                        "thread_messages": len(thread.messages),
                        "events": len(events),
                    },
                )

        agent_ctx = AgentContext(
            new_message=message,
            thread_messages=thread.messages,
            loop=loop,
            events=events,
            coordinator=coordinator,
            recruiter=recruiter,
            client_contact=client_contact,
            candidate=candidate,
        )

        # 5. Run agent engine (classification + optional draft)
        result = await run_agent(agent_ctx, classifier, drafter, parent_trace=trace)
        logger.info(
            "Agent classified message %s as %s, action=%s, confidence=%.2f",
            message_id,
            result.classification.classification,
            result.classification.suggested_action,
            result.classification.confidence,
        )

        # 6. Validate via guardrails
        guard_span = trace.span(name="validate-guardrails") if trace else None

        violations = validate_action(result, loop)

        if guard_span:
            with contextlib.suppress(Exception):
                guard_span.end(output={"violations": violations or []})

        if violations:
            logger.warning(
                "Guardrail violations for message %s: %s",
                message_id,
                violations,
            )
            return

        # 7. Persist suggestion
        persist_span = trace.span(name="persist-suggestion") if trace else None

        suggestion = await agent_service.create_suggestion(
            coordinator_email=coordinator_email,
            loop_id=loop.id if loop else None,
            stage_id=active_stage.id if active_stage else None,
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            classification=result.classification.classification,
            suggested_action=result.classification.suggested_action,
            confidence=result.classification.confidence,
            reasoning=result.classification.reasoning,
            questions=result.classification.questions or None,
            prefilled_data=result.classification.prefilled_data,
        )

        # 8. Persist draft (if any)
        if result.draft:
            await agent_service.create_draft(
                suggestion_id=suggestion.id,
                draft_to=result.draft.to,
                draft_subject=result.draft.subject,
                draft_body=result.draft.body,
                in_reply_to=result.draft.in_reply_to,
            )

        if persist_span:
            with contextlib.suppress(Exception):
                persist_span.end(
                    output={
                        "suggestion_id": suggestion.id,
                        "has_draft": result.draft is not None,
                    },
                )

        logger.info("Suggestion %s created for message %s", suggestion.id, message_id)

        # Set output on the top-level trace
        if trace:
            with contextlib.suppress(Exception):
                trace.update(
                    output={
                        "classification": result.classification.classification.value,
                        "action": result.classification.suggested_action.value,
                        "confidence": result.classification.confidence,
                        "suggestion_id": suggestion.id,
                        "has_draft": result.draft is not None,
                    },
                )

    except Exception:
        logger.exception("Error processing message %s for %s", message_id, coordinator_email)
        if trace:
            with contextlib.suppress(Exception):
                trace.update(level="ERROR", status_message="Exception during processing")
        raise
    finally:
        if langfuse:
            try:
                langfuse.flush()
            except Exception:
                logger.debug("Failed to flush Langfuse", exc_info=True)


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

            # Fetch full message (pre-filter needs headers + body content)
            message = await gmail.get_message(coordinator_email, msg_id)
            thread_id = message.thread_id

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
