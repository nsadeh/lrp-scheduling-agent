"""End-to-end integration test for the email classifier.

Exercises the full pipeline against real infrastructure:
  - Real LLM call (Anthropic via LiteLLM)
  - Real LangFuse prompt fetch + tracing
  - Real Postgres writes to agent_suggestions

Usage:
    cd services/api
    uv run python scripts/test_classifier_e2e.py

Requires: .env with DATABASE_URL, LANGFUSE_*, ANTHROPIC_API_KEY
"""

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from api.ai import init_langfuse, init_llm_service  # noqa: E402
from api.classifier.hook import ClassifierHook  # noqa: E402
from api.classifier.service import SuggestionService  # noqa: E402
from api.gmail.hooks import EmailEvent, MessageDirection, MessageType  # noqa: E402
from api.gmail.models import EmailAddress, Message  # noqa: E402
from api.scheduling.service import LoopService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger("e2e_test")

# ── Test scenarios ───────────────────────────────────────────────────


def scenario_new_interview_request() -> EmailEvent:
    """Client requests to interview a candidate — should produce CREATE_LOOP."""
    return EmailEvent(
        message=Message(
            id="test_msg_001",
            thread_id="test_thread_001",
            subject="Interview Request - John Smith for Analyst Role",
            **{"from": EmailAddress(name="Jane Doe", email="jane.doe@hedgefundcapital.com")},
            to=[EmailAddress(name="Coordinator", email="scheduler@lrp.com")],
            cc=[EmailAddress(name="Mike CM", email="mike@lrp.com")],
            date=datetime(2026, 4, 15, 14, 30, tzinfo=UTC),
            body_text=(
                "Hi,\n\n"
                "I'd like to schedule an interview with John Smith for the "
                "Senior Analyst position. We're looking to move quickly on this.\n\n"
                "Please coordinate with his recruiter to find available times.\n\n"
                "Best,\nJane Doe\nManaging Director, Hedge Fund Capital"
            ),
        ),
        coordinator_email="scheduler@lrp.com",
        direction=MessageDirection.INCOMING,
        message_type=MessageType.NEW_THREAD,
        new_participants=[],
    )


def scenario_availability_response() -> EmailEvent:
    """Recruiter sends candidate availability — should produce ADVANCE_STAGE."""
    return EmailEvent(
        message=Message(
            id="test_msg_002",
            thread_id="test_thread_002",
            subject="Re: John Smith - Round 1 Availability",
            **{"from": EmailAddress(name="Bob Recruiter", email="bob.recruiter@lrp.com")},
            to=[EmailAddress(name="Coordinator", email="scheduler@lrp.com")],
            date=datetime(2026, 4, 15, 15, 0, tzinfo=UTC),
            body_text=(
                "Hi,\n\n"
                "John is available for Round 1 at the following times:\n\n"
                "- Tuesday, April 22nd 2:00-4:00 PM EST\n"
                "- Thursday, April 24th 10:00 AM-12:00 PM EST\n"
                "- Friday, April 25th 3:00-5:00 PM EST\n\n"
                "Let me know which works for the client.\n\n"
                "Bob"
            ),
        ),
        coordinator_email="scheduler@lrp.com",
        direction=MessageDirection.INCOMING,
        message_type=MessageType.REPLY,
        new_participants=[],
    )


def scenario_not_scheduling() -> EmailEvent:
    """Non-scheduling email about compensation — should produce NOT_SCHEDULING."""
    return EmailEvent(
        message=Message(
            id="test_msg_003",
            thread_id="test_thread_003",
            subject="Compensation Discussion - Jane Wilson",
            **{"from": EmailAddress(name="HR Team", email="hr@hedgefundcapital.com")},
            to=[EmailAddress(name="Coordinator", email="scheduler@lrp.com")],
            date=datetime(2026, 4, 15, 16, 0, tzinfo=UTC),
            body_text=(
                "Hi,\n\n"
                "We'd like to discuss the compensation package for Jane Wilson. "
                "Can you pass along her salary expectations? We're thinking base "
                "of $180k with 20% bonus.\n\n"
                "Thanks,\nHR Team"
            ),
        ),
        coordinator_email="scheduler@lrp.com",
        direction=MessageDirection.INCOMING,
        message_type=MessageType.NEW_THREAD,
        new_participants=[],
    )


def scenario_multi_action() -> EmailEvent:
    """Email with two actions — time confirmation + new round request."""
    return EmailEvent(
        message=Message(
            id="test_msg_004",
            thread_id="test_thread_004",
            subject="Re: John Smith - Round 1 Times",
            **{"from": EmailAddress(name="Jane Doe", email="jane.doe@hedgefundcapital.com")},
            to=[EmailAddress(name="Coordinator", email="scheduler@lrp.com")],
            date=datetime(2026, 4, 15, 17, 0, tzinfo=UTC),
            body_text=(
                "Tuesday at 2pm works for Round 1.\n\n"
                "Also, my partner would like to do a Round 2 with John — "
                "can we schedule that as well?\n\n"
                "Jane"
            ),
        ),
        coordinator_email="scheduler@lrp.com",
        direction=MessageDirection.INCOMING,
        message_type=MessageType.REPLY,
        new_participants=[],
    )


def scenario_outgoing_unlinked() -> EmailEvent:
    """Outgoing email on an unlinked thread — should be skipped entirely."""
    return EmailEvent(
        message=Message(
            id="test_msg_005",
            thread_id="test_thread_005",
            subject="Quick question",
            **{"from": EmailAddress(name="Coordinator", email="scheduler@lrp.com")},
            to=[EmailAddress(name="Someone", email="someone@example.com")],
            date=datetime(2026, 4, 15, 18, 0, tzinfo=UTC),
            body_text="Just following up on that thing we discussed.",
        ),
        coordinator_email="scheduler@lrp.com",
        direction=MessageDirection.OUTGOING,
        message_type=MessageType.REPLY,
        new_participants=[],
    )


# ── Test runner ──────────────────────────────────────────────────────

import os  # noqa: E402


async def run_tests():
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()

    langfuse = init_langfuse()
    llm = init_llm_service()

    if not langfuse or not llm:
        logger.error("AI infrastructure not available — check .env")
        return False

    suggestion_svc = SuggestionService(db_pool=pool)
    loop_svc = LoopService(db_pool=pool, gmail=None)

    hook = ClassifierHook(
        llm=llm,
        langfuse=langfuse,
        suggestion_service=suggestion_svc,
        loop_service=loop_svc,
    )

    scenarios = [
        (
            "New interview request",
            scenario_new_interview_request(),
            {
                "expected_classifications": ["new_interview_request"],
                "expected_actions": ["create_loop"],
                "min_suggestions": 1,
            },
        ),
        (
            "Availability response",
            scenario_availability_response(),
            {
                "expected_classifications": ["availability_response"],
                "expected_actions": ["advance_stage", "ask_coordinator", "draft_email"],
                "min_suggestions": 1,
            },
        ),
        (
            "Not scheduling (compensation)",
            scenario_not_scheduling(),
            {
                "expected_classifications": ["not_scheduling"],
                "expected_actions": ["no_action"],
                "min_suggestions": 1,
            },
        ),
        (
            "Multi-action (confirm + new round)",
            scenario_multi_action(),
            {
                "expected_classifications": ["time_confirmation", "new_interview_request"],
                "expected_actions": ["advance_stage", "create_loop", "ask_coordinator"],
                "min_suggestions": 1,  # at least 1, ideally 2
            },
        ),
        (
            "Outgoing on unlinked thread (skip)",
            scenario_outgoing_unlinked(),
            {
                "expected_classifications": [],
                "expected_actions": [],
                "min_suggestions": 0,
            },
        ),
    ]

    passed = 0
    failed = 0

    for name, event, expectations in scenarios:
        logger.info("\n" + "=" * 60)
        logger.info("SCENARIO: %s", name)
        logger.info("=" * 60)

        # Clear test suggestions from prior runs
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM agent_suggestions WHERE gmail_message_id = %(id)s",
                {"id": event.message.id},
            )

        # Run the classifier
        try:
            await hook.on_email(event)
        except Exception:
            logger.exception("  EXCEPTION during classification")
            failed += 1
            continue

        # Check results in DB
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT classification, action, confidence, summary, status, reasoning "
                "FROM agent_suggestions WHERE gmail_message_id = %(id)s "
                "ORDER BY created_at",
                {"id": event.message.id},
            )
            rows = await cur.fetchall()

        logger.info("  Suggestions created: %d", len(rows))
        for i, row in enumerate(rows):
            logger.info(
                "  [%d] classification=%s, action=%s, confidence=%.2f, status=%s",
                i,
                row[0],
                row[1],
                row[2],
                row[4],
            )
            logger.info("      summary: %s", row[3])
            if row[5]:
                # Truncate reasoning for display
                reasoning_preview = row[5][:200] + "..." if len(row[5]) > 200 else row[5]
                logger.info("      reasoning: %s", reasoning_preview)

        # Validate expectations
        ok = True

        # Check min suggestions
        if len(rows) < expectations["min_suggestions"]:
            logger.error(
                "  FAIL: expected >= %d suggestions, got %d",
                expectations["min_suggestions"],
                len(rows),
            )
            ok = False

        # Check classifications (at least one must match)
        if expectations["expected_classifications"]:
            actual_classifications = {r[0] for r in rows}
            if not actual_classifications & set(expectations["expected_classifications"]):
                logger.error(
                    "  FAIL: expected one of %s, got %s",
                    expectations["expected_classifications"],
                    actual_classifications,
                )
                ok = False

        # Check actions (at least one must match)
        if expectations["expected_actions"]:
            actual_actions = {r[1] for r in rows}
            if not actual_actions & set(expectations["expected_actions"]):
                logger.error(
                    "  FAIL: expected one of %s, got %s",
                    expectations["expected_actions"],
                    actual_actions,
                )
                ok = False

        if ok:
            logger.info("  PASS")
            passed += 1
        else:
            failed += 1

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("RESULTS: %d passed, %d failed out of %d scenarios", passed, failed, len(scenarios))
    logger.info("=" * 60)

    # Show total suggestions in DB
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM agent_suggestions")
        row = await cur.fetchone()
        logger.info("Total suggestions in DB: %d", row[0])

    # Flush LangFuse traces
    langfuse.flush()
    langfuse.shutdown()
    await pool.close()

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)
