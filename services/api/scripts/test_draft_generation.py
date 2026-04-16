# ruff: noqa: E501 RUF001
#!/usr/bin/env python3
"""Integration test for the draft-email-v1 LLM endpoint.

Runs real scenarios from coordinator email threads against the published
LangFuse prompt to evaluate draft quality.

Usage:
    cd services/api
    PYTHONPATH=src uv run python3 scripts/test_draft_generation.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api.ai.langfuse_client import init_langfuse  # noqa: E402
from api.ai.llm_service import init_llm_service  # noqa: E402
from api.drafts.endpoint import generate_draft_content  # noqa: E402
from api.drafts.models import GenerateDraftInput  # noqa: E402

# ── Formatting ────────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94mi\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def header(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {BOLD}{title}{RESET}")
    print(f"{'─' * 60}")


def show_draft(body: str, reasoning: str):
    print(f"\n  {BOLD}Generated draft:{RESET}")
    for line in body.split("\n"):
        print(f"    │ {line}")
    print(f"\n  {DIM}Reasoning: {reasoning}{RESET}")


# ── Scenario 1: Share availability with client ────────────────────────────────
# From Claire Cao / BAM thread: recruiter provided availability,
# coordinator drafts email to client (Haley) sharing it.


SCENARIO_1 = GenerateDraftInput(
    draft_directive="Share Claire's availability with the client contact",
    recipient_name="Haley",
    candidate_name="Claire Cao",
    coordinator_name="Fiona",
    extracted_entities=json.dumps(
        {
            "availability": [{"day": "Wednesday 3/18", "slots": ["12pm-2pm"]}],
            "candidate_name": "Claire Cao",
            "timezone": "ET",
        }
    ),
    thread_messages="""\
--- Message (2026-03-16 19:17) ---
From: Andrew West <awest@longridgepartners.com>
Subject: RE: CLAIRE CAO, Millennium Management

Claire Cao – Availability (Times in ET)
Wednesday 3/18: 12pm – 2pm

FYI, she would be very interested to learn more about an opportunity on Eileen's team.

--- Message (2026-03-16 14:00) ---
From: Matt Sullivan <matt@longridgepartners.com>
Subject: RE: CLAIRE CAO, Millennium Management

Will do, thx!

--- Message (2026-03-16 13:59) ---
From: Haley Marlowe <hmarlowe@bamfunds.com>
Subject: RE: CLAIRE CAO, Millennium Management

Hi team,
Can you please share Claire's availability to speak with Kendall this week Wednesday, 3/18, or early next week? This will be for Eileen Chen.
Thank you!
Haley
""",
)

# What Fiona actually wrote:
# "Hi Haley,\nClaire is available (in ET):\nWednesday (3/18): 12-2pm\nThank you,\nFiona"


# ── Scenario 2: Confirm interview + share phone number ────────────────────────
# From Claire Cao / BAM thread: client requested 1:30pm, recruiter confirmed
# with phone number. Coordinator drafts confirmation to client.


SCENARIO_2 = GenerateDraftInput(
    draft_directive="Confirm the interview time and share the candidate's phone number with the client",
    recipient_name="Haley",
    candidate_name="Claire Cao",
    coordinator_name="Fiona",
    extracted_entities=json.dumps(
        {
            "confirmed_time": "1:30pm ET, Wednesday 3/18",
            "phone_number": "267-356-1138",
            "candidate_name": "Claire Cao",
        }
    ),
    thread_messages="""\
--- Message (2026-03-17 09:57) ---
From: Andrew West <awest@longridgepartners.com>
Subject: RE: CLAIRE CAO, Millennium Management

Confirmed, best number to reach her is 267-356-1138

--- Message (2026-03-17 09:47) ---
From: Fiona Campbell <fcampbell@longridgepartners.com>
Subject: RE: CLAIRE CAO, Millennium Management

---------- Forwarded message ---------
From: Haley Marlowe <hmarlowe@bamfunds.com>

Hi Fiona,
Can we please confirm 1:30pmET tomorrow, 3/18?
Thank you!
Haley

--- Message (2026-03-17 09:16) ---
From: Fiona Campbell <fcampbell@longridgepartners.com>
Subject: RE: CLAIRE CAO, Millennium Management

Hi Haley,
Claire is available (in ET):
Wednesday (3/18): 12-2pm
Thank you,
Fiona
""",
)

# What Fiona actually wrote:
# "Confirmed! The best number to reach her is: 267-356-1138."


# ── Scenario 3: Share availability with client (Viking/Biotech thread) ────────
# From Biotech Principal thread: multiple candidates, Fiona shares
# Chris Lo's availability with Olivia at Viking.


SCENARIO_3 = GenerateDraftInput(
    draft_directive="Share Chris and Nick's availability with the client contact. Note we will follow up with Nick's availability shortly.",
    recipient_name="Olivia",
    candidate_name="Chris Lo",
    coordinator_name="Fiona",
    extracted_entities=json.dumps(
        {
            "availability": [
                {
                    "candidate": "Chris Lo",
                    "slots": [
                        {"day": "Friday (3/20)", "times": "12:30-2:30pm, 4-6pm"},
                        {"day": "Monday (3/23)", "times": "9am-1pm"},
                        {"day": "Wednesday (3/25)", "times": "9-11am"},
                    ],
                },
            ],
            "timezone": "ET",
            "note": "Will circle back with Nick's availability shortly",
        }
    ),
    thread_messages="""\
--- Message (2026-03-17 10:41) ---
From: Wilks, Olivia <owilks@vikingglobal.com>
Subject: RE: Biotech Principal

Hi both,
We would like to schedule Chris and Nick with Karim OR Maneka as a next step. Can you please provide upcoming availability?
Best,
Olivia
""",
)

# What Fiona actually wrote:
# "Hi Olivia,\n\nChris is available (in ET):\nFriday (3/20): 12:30-2:30pm, 4-6pm\nMonday (3/23): 9am-1pm\nWednesday (3/25): 9-11am\n\nWe will circle back with Nick's availability shortly, thank you!\n\nBest,\nFiona"


# ── Scenario 4: Confirm + share availability for next round (Viking thread) ───
# Client confirmed Chris with Karim on 3/20. Coordinator confirms receipt
# and shares Nick's availability for his interview with Maneka.


SCENARIO_4 = GenerateDraftInput(
    draft_directive="Confirm receipt of Chris's interview details and share Nick's availability for his interview",
    recipient_name="Olivia",
    candidate_name="Nick Futrell",
    coordinator_name="Fiona",
    extracted_entities=json.dumps(
        {
            "confirmed_interview": {
                "candidate": "Chris Lo",
                "interviewer": "Karim Helmy",
                "time": "12:30-1:00pm, 3/20",
                "zoom_link": "https://vikingglobal.zoom.us/j/84908334356?pwd=67eWF0hC3IHvS1kEaRxMBAoa5xlagg.1",
            },
            "availability": [
                {
                    "candidate": "Nick Futrell",
                    "slots": [
                        {"day": "Tomorrow (3/19)", "times": "9:45-11am, 2:30-4pm"},
                        {"day": "Friday (3/20)", "times": "10-11:30am, 1:30-3:30pm"},
                    ],
                },
            ],
            "timezone": "ET",
        }
    ),
    thread_messages="""\
--- Message (2026-03-17 17:12) ---
From: Wilks, Olivia <owilks@vikingglobal.com>
Subject: RE: Biotech Principal

Thank you! Please find confirmation details for Chris below and kindly confirm receipt.

Interview Schedule, 3/20
12:30 - 1:00pm - Karim Helmy
https://vikingglobal.zoom.us/j/84908334356?pwd=67eWF0hC3IHvS1kEaRxMBAoa5xlagg.1
Meeting ID: 849 0833 4356
Passcode: 516685

--- Message (2026-03-17 16:39) ---
From: Fiona Campbell <fcampbell@longridgepartners.com>
Subject: RE: Biotech Principal

Hi Olivia,
Chris is available (in ET):
Friday (3/20): 12:30-2:30pm, 4-6pm
Monday (3/23): 9am-1pm
Wednesday (3/25): 9-11am

We will circle back with Nick's availability shortly, thank you!
Best,
Fiona
""",
)

# What Fiona actually wrote:
# "Confirmed and shared with Chris! Nick is available (in ET):\nTomorrow (3/19): 9:45-11am, 2:30-4pm\nFriday (3/20): 10-11:30am, 1:30-3:30pm\nThank you,\nFiona"


# ── Run scenarios ─────────────────────────────────────────────────────────────


SCENARIOS = [
    ("Share availability with client (Claire → BAM)", SCENARIO_1),
    ("Confirm interview + phone number (Claire → BAM)", SCENARIO_2),
    ("Share availability (Chris → Viking)", SCENARIO_3),
    ("Confirm + share next availability (Nick → Viking)", SCENARIO_4),
]


async def run_scenario(name: str, input_data: GenerateDraftInput, langfuse, llm):
    header(name)
    print(f"  {DIM}Directive: {input_data.draft_directive}{RESET}")
    print(f"  {DIM}Recipient: {input_data.recipient_name}{RESET}")
    print(f"  {DIM}Candidate: {input_data.candidate_name}{RESET}")

    start = time.monotonic()
    try:
        result = await generate_draft_content(
            llm=llm,
            langfuse=langfuse,
            data=input_data,
        )
        elapsed = time.monotonic() - start
        show_draft(result.body, result.reasoning)
        print(f"\n  {PASS} Generated in {elapsed:.1f}s")
        return True
    except Exception as e:
        elapsed = time.monotonic() - start
        print(f"\n  {FAIL} Failed after {elapsed:.1f}s: {e}")
        return False


async def main():
    print("=" * 60)
    print("  Draft Generation Integration Tests (draft-email-v1)")
    print("=" * 60)

    langfuse = init_langfuse()
    if langfuse is None:
        print(f"\n  {FAIL} LANGFUSE keys not set — cannot run")
        sys.exit(1)

    llm = init_llm_service()
    if llm is None:
        print(f"\n  {FAIL} No LLM provider keys set — cannot run")
        sys.exit(1)

    print(f"\n  {INFO} LangFuse + LLM initialized")

    results = []
    for name, scenario in SCENARIOS:
        ok = await run_scenario(name, scenario, langfuse, llm)
        results.append(ok)

    langfuse.flush()
    langfuse.shutdown()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{total} scenarios completed")
    print(f"{'=' * 60}")

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
