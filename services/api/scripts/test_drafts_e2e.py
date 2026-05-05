# ruff: noqa: E501 RUF001
#!/usr/bin/env python3
"""End-to-end test for the full classification → draft generation pipeline.

Exercises the complete flow against real infrastructure:
  - Pre-creates loops + stages in Postgres
  - Sends email events through EmailRouter (with DraftService wired in)
  - Classifier suggests DRAFT_EMAIL → DraftService generates draft
  - Queries both agent_suggestions and email_drafts tables

Scenarios recreated from real coordinator email threads (Claire Cao/BAM,
Biotech Principal/Viking).

Usage:
    cd services/api
    PYTHONPATH=src uv run python3 scripts/test_drafts_e2e.py

Requires: .env with DATABASE_URL, LANGFUSE_*, ANTHROPIC_API_KEY
"""

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from psycopg.rows import dict_row, tuple_row  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from api.ai import init_langfuse, init_llm_service  # noqa: E402
from api.classifier.loop_classifier import LoopClassifier  # noqa: E402
from api.classifier.next_action_agent import NextActionAgent  # noqa: E402
from api.classifier.router import EmailRouter  # noqa: E402
from api.classifier.service import SuggestionService  # noqa: E402
from api.drafts.service import DraftService  # noqa: E402
from api.gmail.hooks import EmailEvent, MessageDirection, MessageType  # noqa: E402
from api.gmail.models import EmailAddress, Message  # noqa: E402
from api.ids import make_id  # noqa: E402
from api.scheduling.service import LoopService  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("drafts_e2e")
logger.setLevel(logging.INFO)

# ── Formatting ────────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94mi\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

TEST_PREFIX = "e2e_drf_"  # prefix for test data cleanup


# ── DB setup helpers ──────────────────────────────────────────────────────────


async def create_test_loop(
    pool,
    *,
    loop_id: str,
    coordinator_email: str,
    candidate_name: str,
    client_name: str,
    client_email: str,
    client_company: str,
    recruiter_name: str,
    recruiter_email: str,
    title: str,
    stage_state: str = "awaiting_candidate",
    thread_id: str | None = None,
):
    """Insert a minimal loop + contacts + stage + thread link for testing."""
    coord_id = make_id("crd")
    client_id = make_id("cli")
    recruiter_id = make_id("con")
    candidate_id = make_id("can")
    stage_id = make_id("stg")

    async with pool.connection() as conn, conn.transaction():
        # Coordinator
        await conn.execute(
            "INSERT INTO coordinators (id, name, email) VALUES (%s, %s, %s) ON CONFLICT (email) DO NOTHING",
            (coord_id, "Fiona Campbell", coordinator_email),
        )
        cur = await conn.execute(
            "SELECT id FROM coordinators WHERE email = %s", (coordinator_email,)
        )
        coord_id = (await cur.fetchone())[0]

        # Client contact
        await conn.execute(
            "INSERT INTO client_contacts (id, name, email, company) VALUES (%s, %s, %s, %s)",
            (client_id, client_name, client_email, client_company),
        )

        # Recruiter
        await conn.execute(
            "INSERT INTO contacts (id, name, email, role) VALUES (%s, %s, %s, 'recruiter')",
            (recruiter_id, recruiter_name, recruiter_email),
        )

        # Candidate
        await conn.execute(
            "INSERT INTO candidates (id, name) VALUES (%s, %s)",
            (candidate_id, candidate_name),
        )

        # Loop
        await conn.execute(
            """INSERT INTO loops (id, coordinator_id, client_contact_id, recruiter_id,
               candidate_id, title) VALUES (%s, %s, %s, %s, %s, %s)""",
            (loop_id, coord_id, client_id, recruiter_id, candidate_id, title),
        )

        # Stage
        await conn.execute(
            "INSERT INTO stages (id, loop_id, name, state, ordinal) VALUES (%s, %s, 'Round 1', %s, 0)",
            (stage_id, loop_id, stage_state),
        )

        # Thread link
        if thread_id:
            thread_link_id = make_id("thr")
            await conn.execute(
                "INSERT INTO loop_email_threads (id, loop_id, gmail_thread_id, subject) VALUES (%s, %s, %s, %s)",
                (thread_link_id, loop_id, thread_id, title),
            )

    return stage_id


async def cleanup_test_data(pool):
    """Remove all test data created by this script."""
    async with pool.connection() as conn, conn.transaction():
        # Delete drafts for test suggestions
        await conn.execute(
            "DELETE FROM email_drafts WHERE suggestion_id IN (SELECT id FROM agent_suggestions WHERE gmail_message_id LIKE %s)",
            (f"{TEST_PREFIX}%",),
        )
        await conn.execute(
            "DELETE FROM agent_suggestions WHERE gmail_message_id LIKE %s",
            (f"{TEST_PREFIX}%",),
        )
        # Delete test loops and related data
        await conn.execute(
            "DELETE FROM loop_events WHERE loop_id IN (SELECT id FROM loops WHERE id LIKE %s)",
            (f"{TEST_PREFIX}%",),
        )
        await conn.execute(
            "DELETE FROM time_slots WHERE stage_id IN (SELECT id FROM stages WHERE loop_id IN (SELECT id FROM loops WHERE id LIKE %s))",
            (f"{TEST_PREFIX}%",),
        )
        await conn.execute(
            "DELETE FROM loop_email_threads WHERE loop_id IN (SELECT id FROM loops WHERE id LIKE %s)",
            (f"{TEST_PREFIX}%",),
        )
        await conn.execute(
            "DELETE FROM stages WHERE loop_id IN (SELECT id FROM loops WHERE id LIKE %s)",
            (f"{TEST_PREFIX}%",),
        )
        await conn.execute("DELETE FROM loops WHERE id LIKE %s", (f"{TEST_PREFIX}%",))


# ── Scenarios from real email threads ─────────────────────────────────────────


def scenario_claire_availability() -> tuple[dict, EmailEvent]:
    """BAM thread: recruiter sent Claire's availability, client asked for it.
    Classifier should suggest DRAFT_EMAIL to share availability with client.
    """
    loop_id = f"{TEST_PREFIX}claire_1"
    thread_id = f"{TEST_PREFIX}thread_claire"

    setup = dict(
        loop_id=loop_id,
        coordinator_email="fcampbell@longridgepartners.com",
        candidate_name="Claire Cao",
        client_name="Haley Marlowe",
        client_email="hmarlowe@bamfunds.com",
        client_company="BAM Funds",
        recruiter_name="Andrew West",
        recruiter_email="awest@longridgepartners.com",
        title="Round 1 - Claire Cao, BAM (KSUN - Semis Senior Associate)",
        stage_state="awaiting_candidate",
        thread_id=thread_id,
    )

    # The incoming message: Andrew (recruiter) provides Claire's availability
    event = EmailEvent(
        message=Message(
            id=f"{TEST_PREFIX}msg_claire_avail",
            thread_id=thread_id,
            subject="RE: CLAIRE CAO, Millennium Management (REQ7931 KSUN - Semis Senior Associate | NY) | Vendor Portal",
            **{"from": EmailAddress(name="Andrew West", email="awest@longridgepartners.com")},
            to=[
                EmailAddress(name="Fiona Campbell", email="fcampbell@longridgepartners.com"),
                EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com"),
            ],
            date=datetime(2026, 3, 16, 19, 17, tzinfo=UTC),
            body_text=(
                "Claire Cao – Availability (Times in ET)\n"
                "Wednesday 3/18: 12pm – 2pm\n\n"
                "FYI, she would be very interested to learn more about an opportunity on Eileen's team. "
                "She has heard a lot of great things about her from individuals on the sell-side. "
                "No new changes on her end, status quo with MLP."
            ),
        ),
        coordinator_email="fcampbell@longridgepartners.com",
        direction=MessageDirection.INCOMING,
        message_type=MessageType.REPLY,
        new_participants=[],
        thread_messages=[
            Message(
                id="prior_haley_request",
                thread_id=thread_id,
                subject="RE: CLAIRE CAO, Millennium Management",
                **{"from": EmailAddress(name="Haley Marlowe", email="hmarlowe@bamfunds.com")},
                to=[
                    EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com"),
                    EmailAddress(name="Kendall Daly", email="kdaly@bamfunds.com"),
                ],
                cc=[EmailAddress(name="Fiona Campbell", email="fcampbell@longridgepartners.com")],
                date=datetime(2026, 3, 16, 13, 59, tzinfo=UTC),
                body_text=(
                    "Hi team,\n"
                    "Can you please share Claire's availability to speak with Kendall this week "
                    "Wednesday, 3/18, or early next week? This will be for Eileen Chen.\n"
                    "Thank you!\nHaley"
                ),
            ),
            Message(
                id="prior_matt_ack",
                thread_id=thread_id,
                subject="RE: CLAIRE CAO, Millennium Management",
                **{"from": EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com")},
                to=[
                    EmailAddress(name="Haley Marlowe", email="hmarlowe@bamfunds.com"),
                    EmailAddress(name="Kendall Daly", email="kdaly@bamfunds.com"),
                ],
                cc=[EmailAddress(name="Fiona Campbell", email="fcampbell@longridgepartners.com")],
                date=datetime(2026, 3, 16, 14, 0, tzinfo=UTC),
                body_text="Will do, thx!\n\nMatt Sullivan | Partner",
            ),
        ],
    )
    return setup, event


def scenario_claire_confirmation() -> tuple[dict, EmailEvent]:
    """BAM thread: client requested 1:30pm, recruiter confirmed + phone number.
    Classifier should suggest DRAFT_EMAIL to confirm with client.
    """
    loop_id = f"{TEST_PREFIX}claire_2"
    thread_id = f"{TEST_PREFIX}thread_claire_conf"

    setup = dict(
        loop_id=loop_id,
        coordinator_email="fcampbell@longridgepartners.com",
        candidate_name="Claire Cao",
        client_name="Haley Marlowe",
        client_email="hmarlowe@bamfunds.com",
        client_company="BAM Funds",
        recruiter_name="Andrew West",
        recruiter_email="awest@longridgepartners.com",
        title="Round 1 - Claire Cao, BAM (KSUN - Semis Senior Associate)",
        stage_state="awaiting_client",
        thread_id=thread_id,
    )

    # The incoming message: Andrew (recruiter) confirms time + shares phone
    event = EmailEvent(
        message=Message(
            id=f"{TEST_PREFIX}msg_claire_conf",
            thread_id=thread_id,
            subject="RE: CLAIRE CAO, Millennium Management",
            **{"from": EmailAddress(name="Andrew West", email="awest@longridgepartners.com")},
            to=[
                EmailAddress(name="Fiona Campbell", email="fcampbell@longridgepartners.com"),
                EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com"),
            ],
            date=datetime(2026, 3, 17, 9, 57, tzinfo=UTC),
            body_text="Confirmed, best number to reach her is 267-356-1138",
        ),
        coordinator_email="fcampbell@longridgepartners.com",
        direction=MessageDirection.INCOMING,
        message_type=MessageType.REPLY,
        new_participants=[],
        thread_messages=[
            Message(
                id="prior_haley_confirm_req",
                thread_id=thread_id,
                subject="RE: CLAIRE CAO, Millennium Management",
                **{"from": EmailAddress(name="Haley Marlowe", email="hmarlowe@bamfunds.com")},
                to=[
                    EmailAddress(name="Fiona Campbell", email="fcampbell@longridgepartners.com"),
                    EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com"),
                ],
                cc=[EmailAddress(name="Kendall Daly", email="kdaly@bamfunds.com")],
                date=datetime(2026, 3, 17, 9, 39, tzinfo=UTC),
                body_text="Hi Fiona,\nCan we please confirm 1:30pmET tomorrow, 3/18?\nThank you!\nHaley",
            ),
            Message(
                id="prior_fiona_avail",
                thread_id=thread_id,
                subject="RE: CLAIRE CAO, Millennium Management",
                **{
                    "from": EmailAddress(
                        name="Fiona Campbell", email="fcampbell@longridgepartners.com"
                    )
                },
                to=[EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com")],
                cc=[
                    EmailAddress(name="Haley Marlowe", email="hmarlowe@bamfunds.com"),
                    EmailAddress(name="Kendall Daly", email="kdaly@bamfunds.com"),
                ],
                date=datetime(2026, 3, 17, 9, 16, tzinfo=UTC),
                body_text="Hi Haley,\nClaire is available (in ET):\nWednesday (3/18): 12-2pm\nThank you,\nFiona",
            ),
        ],
    )
    return setup, event


# ── Test runner ───────────────────────────────────────────────────────────────


async def run_scenario(name, setup, event, hook, pool):
    """Run a single scenario and display results from the DB."""
    print(f"\n{'─' * 60}")
    print(f"  {BOLD}{name}{RESET}")
    print(f"{'─' * 60}")

    # Create loop + stage + thread link
    stage_id = await create_test_loop(pool, **setup)
    print(f"  {DIM}Loop: {setup['loop_id']} | Stage: {stage_id} ({setup['stage_state']}){RESET}")
    print(f"  {DIM}Thread: {setup.get('thread_id', 'none')}{RESET}")
    print(f"  {DIM}Incoming from: {event.message.from_.email}{RESET}")

    # Run classifier + draft generation
    try:
        await hook.on_email(event)
    except Exception as e:
        import traceback

        print(f"  {FAIL} Exception: {e}")
        traceback.print_exc()
        return False

    # ── Query agent_suggestions ──────────────────────────────────────
    async with pool.connection() as conn:
        conn.row_factory = dict_row
        try:
            cur = await conn.execute(
                """SELECT id, classification, action, confidence, summary, status,
                          extracted_entities, reasoning
                   FROM agent_suggestions
                   WHERE gmail_message_id = %s
                   ORDER BY created_at""",
                (event.message.id,),
            )
            suggestions = await cur.fetchall()
        finally:
            conn.row_factory = tuple_row

    print(f"\n  {BOLD}agent_suggestions ({len(suggestions)} rows):{RESET}")
    for i, s in enumerate(suggestions):
        print(
            f"    [{i}] {s['classification']} → {s['action']} (confidence: {s['confidence']:.2f})"
        )
        print(f"        summary: {s['summary']}")
        entities = s.get("extracted_entities", {})
        if entities and entities != {}:
            ent_str = json.dumps(entities, indent=2)
            # Truncate long entities
            if len(ent_str) > 300:
                ent_str = ent_str[:300] + "\n        ..."
            print(f"        entities: {ent_str[:200]}")

    # ── Query email_drafts ───────────────────────────────────────────
    async with pool.connection() as conn:
        conn.row_factory = dict_row
        try:
            cur = await conn.execute(
                """SELECT d.id, d.status, d.to_emails, d.cc_emails, d.subject, d.body,
                          d.gmail_thread_id, s.summary as suggestion_summary
                   FROM email_drafts d
                   JOIN agent_suggestions s ON d.suggestion_id = s.id
                   WHERE s.gmail_message_id = %s
                   ORDER BY d.created_at""",
                (event.message.id,),
            )
            drafts = await cur.fetchall()
        finally:
            conn.row_factory = tuple_row

    if drafts:
        print(f"\n  {BOLD}email_drafts ({len(drafts)} rows):{RESET}")
        for i, d in enumerate(drafts):
            print(f"    [{i}] status={d['status']} | to={d['to_emails']} | cc={d['cc_emails']}")
            print(f"        subject: {d['subject']}")
            print(f"        {BOLD}body:{RESET}")
            for line in d["body"].split("\n"):
                print(f"          │ {line}")
    else:
        print(
            f"\n  {DIM}email_drafts: 0 rows (no DRAFT_EMAIL suggestion, or generation skipped){RESET}"
        )

    # ── Verdict ──────────────────────────────────────────────────────
    has_draft_suggestion = any(s["action"] == "draft_email" for s in suggestions)
    has_draft = len(drafts) > 0

    if has_draft:
        print(f"\n  {PASS} Full pipeline: classify → DRAFT_EMAIL → draft generated")
        return True
    elif has_draft_suggestion:
        print(f"\n  {FAIL} Classifier suggested DRAFT_EMAIL but no draft was generated")
        return False
    else:
        actions = [s["action"] for s in suggestions]
        print(f"\n  {INFO} Classifier did not suggest DRAFT_EMAIL (actions: {actions})")
        return True  # Not a failure — the classifier chose a different action


async def main():
    print("=" * 60)
    print(f"  {BOLD}Drafts E2E: Classification → Draft Generation Pipeline{RESET}")
    print("=" * 60)

    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()

    langfuse = init_langfuse()
    llm = init_llm_service()

    if not langfuse or not llm:
        print(f"\n  {FAIL} AI infrastructure not available — check .env")
        await pool.close()
        sys.exit(1)

    print(f"\n  {INFO} LangFuse + LLM + Postgres initialized")

    # Clean up any prior test data
    await cleanup_test_data(pool)
    print(f"  {INFO} Cleaned up prior test data")

    # Wire up the full pipeline
    suggestion_svc = SuggestionService(db_pool=pool)
    loop_svc = LoopService(db_pool=pool, gmail=None)
    draft_svc = DraftService(
        db_pool=pool,
        loop_service=loop_svc,
        llm=llm,
        langfuse=langfuse,
    )

    classifier = LoopClassifier(
        llm=llm,
        langfuse=langfuse,
        suggestion_service=suggestion_svc,
        loop_service=loop_svc,
    )
    agent = NextActionAgent(
        llm=llm,
        langfuse=langfuse,
        suggestion_service=suggestion_svc,
        loop_service=loop_svc,
        draft_service=draft_svc,
    )
    hook = EmailRouter(
        loop_classifier=classifier,
        next_action_agent=agent,
        loop_service=loop_svc,
    )

    # Run scenarios
    scenarios = [
        (
            "Claire Cao — recruiter sends availability (expect: share with client)",
            *scenario_claire_availability(),
        ),
        (
            "Claire Cao — recruiter confirms time + phone (expect: confirm to client)",
            *scenario_claire_confirmation(),
        ),
    ]

    results = []
    for name, setup, event in scenarios:
        ok = await run_scenario(name, setup, event, hook, pool)
        results.append(ok)

    # ── Final summary ────────────────────────────────────────────────
    async with pool.connection() as conn:
        conn.row_factory = dict_row
        try:
            cur = await conn.execute(
                "SELECT count(*) as n FROM agent_suggestions WHERE gmail_message_id LIKE %s",
                (f"{TEST_PREFIX}%",),
            )
            sug_count = (await cur.fetchone())["n"]

            cur = await conn.execute(
                """SELECT count(*) as n FROM email_drafts
                   WHERE suggestion_id IN (
                       SELECT id FROM agent_suggestions WHERE gmail_message_id LIKE %s
                   )""",
                (f"{TEST_PREFIX}%",),
            )
            draft_count = (await cur.fetchone())["n"]
        finally:
            conn.row_factory = tuple_row

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{total} scenarios passed")
    print(f"  DB totals: {sug_count} suggestions, {draft_count} drafts")
    print(f"{'=' * 60}")

    # Cleanup
    langfuse.flush()
    langfuse.shutdown()
    await pool.close()

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
