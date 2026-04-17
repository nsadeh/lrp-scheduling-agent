"""Integration test: background reclassification after loop creation.

Validates that the arq worker correctly reclassifies emails after a loop
is created, producing follow-up suggestions (e.g. DRAFT_EMAIL).

Usage:
    cd services/api
    uv run python scripts/test_background_reclassify.py

Requires:
  - Local Postgres with data (docker compose up -d)
  - .env with DATABASE_URL, LANGFUSE_*, ANTHROPIC_API_KEY
  - At least one coordinator with Gmail credentials in the DB
  - The arq worker running (./scripts/dev-api.sh starts it)

What it does:
  1. Finds a coordinator with stored Gmail credentials
  2. Lists their recent Gmail threads, finds one NOT linked to a loop
  3. Inserts a CREATE_LOOP suggestion for that thread
  4. Prints instructions to test in the Gmail sidebar UI
  5. Polls for new suggestions (from background reclassification) for 90s
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import os  # noqa: E402

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from api.classifier.models import SuggestionItem  # noqa: E402
from api.classifier.service import SuggestionService  # noqa: E402
from api.gmail.auth import TokenStore  # noqa: E402
from api.gmail.client import GmailClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger("test_bg_reclassify")


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")

    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()

    try:
        # ── Step 1: Find a coordinator with Gmail credentials ──────────
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT user_email FROM gmail_tokens LIMIT 1")
            row = await cur.fetchone()
        if not row:
            logger.error("No coordinators with Gmail credentials found. Run OAuth flow first.")
            return
        coordinator_email = row[0]
        logger.info("Using coordinator: %s", coordinator_email)

        # ── Step 2: Find a thread NOT linked to a loop ─────────────────
        token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
        gmail = GmailClient(token_store)

        # Find threads we've seen (via suggestions or processed_messages) that
        # are NOT linked to a loop yet. These are the ones that would have
        # CREATE_LOOP suggestions in the real flow.
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT DISTINCT s.gmail_thread_id, s.gmail_message_id
                FROM agent_suggestions s
                LEFT JOIN loop_email_threads let ON let.gmail_thread_id = s.gmail_thread_id
                WHERE s.coordinator_email = %(email)s
                  AND let.loop_id IS NULL
                ORDER BY s.gmail_thread_id
                LIMIT 5
            """,
                {"email": coordinator_email},
            )
            unlinked_rows = await cur.fetchall()

        # If no suggestions exist for unlinked threads, try processed_messages
        if not unlinked_rows:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    """
                    SELECT DISTINCT pm.gmail_message_id
                    FROM processed_messages pm
                    LEFT JOIN loop_email_threads let
                        ON let.gmail_thread_id = (
                            SELECT gmail_thread_id FROM agent_suggestions
                            WHERE gmail_message_id = pm.gmail_message_id LIMIT 1
                        )
                    WHERE pm.coordinator_email = %(email)s
                      AND let.loop_id IS NULL
                    LIMIT 5
                """,
                    {"email": coordinator_email},
                )
                pm_rows = await cur.fetchall()
                if pm_rows:
                    # Fetch message to get thread_id
                    for pm_row in pm_rows:
                        try:
                            msg = await gmail.get_message(coordinator_email, pm_row[0])
                            unlinked_rows = [(msg.thread_id, msg.id)]
                            break
                        except Exception:
                            continue

        if not unlinked_rows:
            logger.error(
                "No unlinked threads found for %s. "
                "Send an email to this coordinator to create one.",
                coordinator_email,
            )
            return

        unlinked_thread = unlinked_rows[0][0]
        msg_id_hint = unlinked_rows[0][1]

        # Fetch thread details from Gmail
        logger.info("Fetching thread %s from Gmail...", unlinked_thread)
        try:
            thread_data = await gmail.get_thread(coordinator_email, unlinked_thread)
        except Exception:
            logger.exception("Failed to fetch thread %s", unlinked_thread)
            return

        if not thread_data.messages:
            logger.error("Thread %s has no messages", unlinked_thread)
            return

        # Use the specific message if available, otherwise latest
        message = thread_data.messages[-1]
        if msg_id_hint:
            for m in thread_data.messages:
                if m.id == msg_id_hint:
                    message = m
                    break
        logger.info(
            "Found unlinked thread: %s (subject: %s, message: %s)",
            unlinked_thread,
            message.subject,
            message.id,
        )

        # ── Step 3: Insert a CREATE_LOOP suggestion ────────────────────
        suggestion_svc = SuggestionService(db_pool=pool)
        item = SuggestionItem(
            classification="new_interview_request",
            action="create_loop",
            confidence=0.95,
            summary=f"New interview request: {message.subject}",
            extracted_entities={
                "candidate_name": "Test Candidate",
                "client_name": message.from_.name if message.from_ else "Unknown Client",
                "client_email": message.from_.email if message.from_ else "",
                "client_company": "Test Company",
                "recruiter_name": "Test Recruiter",
                "recruiter_email": coordinator_email,
            },
        )

        suggestion = await suggestion_svc.create_suggestion(
            coordinator_email=coordinator_email,
            gmail_message_id=message.id,
            gmail_thread_id=unlinked_thread,
            item=item,
            reasoning="Test suggestion for background reclassification validation",
        )
        logger.info("Created CREATE_LOOP suggestion: %s", suggestion.id)

        # ── Step 4: Print instructions ─────────────────────────────────
        print("\n" + "=" * 70)
        print("  BACKGROUND RECLASSIFICATION TEST")
        print("=" * 70)
        print(f"\n  Coordinator:  {coordinator_email}")
        print(f"  Thread:       {unlinked_thread}")
        print(f"  Subject:      {message.subject}")
        print(f"  Suggestion:   {suggestion.id}")
        print("\n  Steps:")
        print(f"  1. Open Gmail and find the thread: '{message.subject}'")
        print("  2. Open the sidebar — you should see a CREATE_LOOP suggestion")
        print("  3. Click 'Create Loop' and fill in details")
        print("  4. ✅ The UI should return INSTANTLY (no spinner delay)")
        print("  5. Wait ~10-30s, then click 'Refresh' in the sidebar")
        print("  6. ✅ New suggestions (e.g. DRAFT_EMAIL) should appear")
        print("\n  Polling for new suggestions on this thread for 90s...")
        print("=" * 70 + "\n")

        # ── Step 5: Poll for new suggestions ───────────────────────────
        initial_count = len(await suggestion_svc.get_suggestions_for_thread(unlinked_thread))
        logger.info("Initial suggestion count for thread: %d", initial_count)

        start = time.monotonic()
        timeout = 90
        poll_interval = 5
        found_new = False

        while time.monotonic() - start < timeout:
            await asyncio.sleep(poll_interval)
            current = await suggestion_svc.get_suggestions_for_thread(unlinked_thread)
            current_count = len(current)
            elapsed = int(time.monotonic() - start)

            if current_count > initial_count:
                new_suggestions = current[: current_count - initial_count]
                logger.info(
                    "✅ Found %d new suggestion(s) after %ds!",
                    current_count - initial_count,
                    elapsed,
                )
                for s in new_suggestions:
                    logger.info(
                        "  → %s: %s (action=%s, confidence=%.2f)",
                        s.id,
                        s.summary,
                        s.action,
                        s.confidence,
                    )
                found_new = True
                break
            else:
                logger.info(
                    "  [%ds/%ds] Still %d suggestion(s) — waiting for background worker...",
                    elapsed,
                    timeout,
                    current_count,
                )

        if not found_new:
            logger.warning(
                "⚠️  No new suggestions after %ds. Possible causes:\n"
                "  - arq worker not running (check: ./scripts/dev-api.sh)\n"
                "  - Redis not available (check: docker compose up -d)\n"
                "  - Loop was not created in the UI yet (create it and re-run)\n"
                "  - LLM API errors (check worker logs)",
                timeout,
            )

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
