# ruff: noqa: E501 RUF001
#!/usr/bin/env python3
"""Upload classifier eval dataset to LangFuse.

Creates a dataset with items matching the ClassifyEmailInput schema
(input) and ClassificationResult schema (expected_output). These can
be used for LangFuse experiments to evaluate prompt changes.

Usage:
    cd services/api
    PYTHONPATH=src uv run python3 scripts/upload_classifier_dataset.py
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api.ai.langfuse_client import init_langfuse  # noqa: E402
from api.classifier.endpoint import ClassifyEmailInput  # noqa: E402
from api.classifier.formatters import (  # noqa: E402
    format_email,
    format_stage_states,
    format_thread_history,
    format_transitions,
)
from api.classifier.models import (  # noqa: E402
    ClassificationResult,
    EmailClassification,
    SuggestedAction,
    SuggestionItem,
)
from api.gmail.hooks import MessageDirection, MessageType  # noqa: E402
from api.gmail.models import EmailAddress, Message  # noqa: E402
from api.scheduling.models import StageState  # noqa: E402

# ── Formatting ────────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94mi\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

DATASET_NAME = "classifier-eval-v3"

# Shared state machine context (same for all items)
STAGE_STATES = format_stage_states()
TRANSITIONS = format_transitions()


# ── Helper to build ClassifyEmailInput from raw scenario data ─────────────────


def build_input(
    *,
    msg: Message,
    direction: MessageDirection,
    message_type: MessageType,
    thread_messages: list[Message],
    loop_state: str,
    active_loops_summary: str = "No active loops for this coordinator.",
    events: str = "No events recorded for this loop.",
) -> dict:
    """Build a ClassifyEmailInput dict matching the Pydantic schema."""
    if thread_messages:
        thread_history = format_thread_history(thread_messages, msg.id)
    else:
        thread_history = "No prior messages in this thread."

    input_model = ClassifyEmailInput(
        stage_states=STAGE_STATES,
        transitions=TRANSITIONS,
        email=format_email(msg, direction.value, message_type.value),
        thread_history=thread_history,
        loop_state=loop_state,
        active_loops_summary=active_loops_summary,
        events=events,
        direction=direction.value,
    )
    return input_model.model_dump()


# ── Dataset items ─────────────────────────────────────────────────────────────

from datetime import UTC, datetime  # noqa: E402


def item_claire_availability() -> tuple[str, dict, dict, dict]:
    """Recruiter provides availability → advance_stage + draft_email."""
    thread_id = "thread_claire"

    msg = Message(
        id="msg_claire_avail",
        thread_id=thread_id,
        subject="RE: CLAIRE CAO, Millennium Management (REQ7931 KSUN - Semis Senior Associate | NY)",
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
    )

    thread_messages = [
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
            body_text="Hi team,\nCan you please share Claire's availability to speak with Kendall this week Wednesday, 3/18, or early next week? This will be for Eileen Chen.\nThank you!\nHaley",
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
    ]

    loop_state = (
        "Loop: Round 1 - Claire Cao, BAM (KSUN - Semis Senior Associate) (ID: lop_claire_1)\n"
        "Candidate: Claire Cao\n"
        "Client: Haley Marlowe (BAM Funds)\n"
        "Recruiter: Andrew West <awest@longridgepartners.com>\n\n"
        "Stages:\n"
        "  - Round 1 (ID: stg_1): awaiting_candidate"
    )

    input_data = build_input(
        msg=msg,
        direction=MessageDirection.INCOMING,
        message_type=MessageType.REPLY,
        thread_messages=thread_messages,
        loop_state=loop_state,
    )

    expected_output = ClassificationResult(
        suggestions=[
            SuggestionItem(
                classification=EmailClassification.AVAILABILITY_RESPONSE,
                action=SuggestedAction.ADVANCE_STAGE,
                confidence=0.95,
                summary="Claire Cao's availability received. Advance to awaiting_client.",
                target_state=StageState.AWAITING_CLIENT,
                target_stage_id="stg_1",
                extracted_entities={
                    "candidate_name": "Claire Cao",
                    "client_company": "BAM Funds",
                    "time_slots": ["2026-03-18T12:00:00-04:00/2026-03-18T14:00:00-04:00"],
                },
            ),
            SuggestionItem(
                classification=EmailClassification.AVAILABILITY_RESPONSE,
                action=SuggestedAction.DRAFT_EMAIL,
                confidence=0.95,
                summary="Draft email to share Claire's availability with the client.",
                action_data={
                    "directive": "Share Claire Cao's availability with the client",
                    "recipient_type": "client",
                },
                extracted_entities={
                    "candidate_name": "Claire Cao",
                    "time_slots": ["2026-03-18T12:00:00-04:00/2026-03-18T14:00:00-04:00"],
                },
            ),
        ],
        reasoning="The recruiter provided Claire's availability for Wednesday 3/18 12-2pm ET. Two actions needed: advance the stage from awaiting_candidate to awaiting_client, and draft an email to the client (Haley) sharing the availability.",
    ).model_dump()

    metadata = {
        "source": "real_thread",
        "thread": "Claire Cao / BAM Funds",
        "scenario": "recruiter_provides_availability",
        "coordinator": "Fiona Campbell",
    }

    return "claire_availability", input_data, expected_output, metadata


def item_claire_confirmation() -> tuple[str, dict, dict, dict]:
    """Recruiter confirms time + phone → advance_stage + draft_email."""
    thread_id = "thread_claire_conf"

    msg = Message(
        id="msg_claire_conf",
        thread_id=thread_id,
        subject="RE: CLAIRE CAO, Millennium Management",
        **{"from": EmailAddress(name="Andrew West", email="awest@longridgepartners.com")},
        to=[
            EmailAddress(name="Fiona Campbell", email="fcampbell@longridgepartners.com"),
            EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com"),
        ],
        date=datetime(2026, 3, 17, 9, 57, tzinfo=UTC),
        body_text="Confirmed, best number to reach her is 267-356-1138",
    )

    thread_messages = [
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
                "from": EmailAddress(name="Fiona Campbell", email="fcampbell@longridgepartners.com")
            },
            to=[EmailAddress(name="Matt Sullivan", email="matt@longridgepartners.com")],
            cc=[
                EmailAddress(name="Haley Marlowe", email="hmarlowe@bamfunds.com"),
                EmailAddress(name="Kendall Daly", email="kdaly@bamfunds.com"),
            ],
            date=datetime(2026, 3, 17, 9, 16, tzinfo=UTC),
            body_text="Hi Haley,\nClaire is available (in ET):\nWednesday (3/18): 12-2pm\nThank you,\nFiona",
        ),
    ]

    loop_state = (
        "Loop: Round 1 - Claire Cao, BAM (KSUN - Semis Senior Associate) (ID: lop_claire_2)\n"
        "Candidate: Claire Cao\n"
        "Client: Haley Marlowe (BAM Funds)\n"
        "Recruiter: Andrew West <awest@longridgepartners.com>\n\n"
        "Stages:\n"
        "  - Round 1 (ID: stg_2): awaiting_client"
    )

    input_data = build_input(
        msg=msg,
        direction=MessageDirection.INCOMING,
        message_type=MessageType.REPLY,
        thread_messages=thread_messages,
        loop_state=loop_state,
    )

    expected_output = ClassificationResult(
        suggestions=[
            SuggestionItem(
                classification=EmailClassification.TIME_CONFIRMATION,
                action=SuggestedAction.ADVANCE_STAGE,
                confidence=0.95,
                summary="Interview confirmed for 1:30pm ET on 3/18. Advance to scheduled.",
                target_state=StageState.SCHEDULED,
                target_stage_id="stg_2",
                extracted_entities={
                    "candidate_name": "Claire Cao",
                    "client_company": "BAM Funds",
                    "time_slots": ["2026-03-18T13:30:00-04:00"],
                    "phone_number": "267-356-1138",
                },
            ),
            SuggestionItem(
                classification=EmailClassification.TIME_CONFIRMATION,
                action=SuggestedAction.DRAFT_EMAIL,
                confidence=0.95,
                summary="Draft confirmation email to client with phone number.",
                action_data={
                    "directive": "Confirm the interview time and share the candidate's phone number with the client",
                    "recipient_type": "client",
                },
                extracted_entities={
                    "candidate_name": "Claire Cao",
                    "time_slots": ["2026-03-18T13:30:00-04:00"],
                    "phone_number": "267-356-1138",
                },
            ),
        ],
        reasoning="The recruiter confirmed the 1:30pm time and provided Claire's phone number. Two actions: advance to scheduled, and draft a confirmation email to Haley with the phone number.",
    ).model_dump()

    metadata = {
        "source": "real_thread",
        "thread": "Claire Cao / BAM Funds",
        "scenario": "recruiter_confirms_time_and_phone",
        "coordinator": "Fiona Campbell",
    }

    return "claire_confirmation", input_data, expected_output, metadata


def item_not_scheduling() -> tuple[str, dict, dict, dict]:
    """Compensation discussion — not scheduling, no action."""
    msg = Message(
        id="msg_not_sched",
        thread_id="thread_not_sched",
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
    )

    input_data = build_input(
        msg=msg,
        direction=MessageDirection.INCOMING,
        message_type=MessageType.NEW_THREAD,
        thread_messages=[],
        loop_state="No matching loop found for this thread.",
    )

    expected_output = ClassificationResult(
        suggestions=[
            SuggestionItem(
                classification=EmailClassification.NOT_SCHEDULING,
                action=SuggestedAction.NO_ACTION,
                confidence=0.95,
                summary="Compensation discussion — not related to interview scheduling.",
            ),
        ],
        reasoning="This email is about compensation negotiation, not interview scheduling. No scheduling action needed.",
    ).model_dump()

    metadata = {
        "source": "synthetic",
        "scenario": "not_scheduling_compensation",
    }

    return "not_scheduling_compensation", input_data, expected_output, metadata


def item_new_interview_request() -> tuple[str, dict, dict, dict]:
    """Client requests to interview a candidate — create_loop on unlinked thread."""
    msg = Message(
        id="msg_new_request",
        thread_id="thread_new_request",
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
    )

    input_data = build_input(
        msg=msg,
        direction=MessageDirection.INCOMING,
        message_type=MessageType.NEW_THREAD,
        thread_messages=[],
        loop_state="No matching loop found for this thread.",
    )

    expected_output = ClassificationResult(
        suggestions=[
            SuggestionItem(
                classification=EmailClassification.NEW_INTERVIEW_REQUEST,
                action=SuggestedAction.CREATE_LOOP,
                confidence=0.9,
                summary="New interview request for John Smith at Hedge Fund Capital.",
                extracted_entities={
                    "candidate_name": "John Smith",
                    "client_company": "Hedge Fund Capital",
                },
            ),
        ],
        reasoning="This is a new interview request from a client for John Smith. No existing loop found — suggest creating one.",
    ).model_dump()

    metadata = {
        "source": "synthetic",
        "scenario": "new_interview_request",
    }

    return "new_interview_request", input_data, expected_output, metadata


# ── Upload ────────────────────────────────────────────────────────────────────

ITEMS = [
    item_claire_availability,
    item_claire_confirmation,
    item_not_scheduling,
    item_new_interview_request,
]


def main():
    print(f"{'=' * 60}")
    print(f"  {BOLD}Upload Classifier Eval Dataset to LangFuse{RESET}")
    print(f"{'=' * 60}")

    langfuse = init_langfuse()
    if langfuse is None:
        print(f"\n  {FAIL} LANGFUSE keys not set — cannot upload")
        sys.exit(1)

    # Create or update dataset
    langfuse.create_dataset(
        name=DATASET_NAME,
        description=(
            "Eval dataset for scheduling-classifier-v3. "
            "Items from real coordinator email threads (Claire Cao/BAM, Biotech/Viking) "
            "and synthetic scenarios. Input matches ClassifyEmailInput schema, "
            "expected_output matches ClassificationResult schema."
        ),
        metadata={
            "prompt": "scheduling-classifier-v3",
            "input_type": "ClassifyEmailInput",
            "output_type": "ClassificationResult",
        },
    )
    print(f"\n  {PASS} Dataset '{DATASET_NAME}' created/updated")

    # Upload items
    for item_fn in ITEMS:
        name, input_data, expected_output, metadata = item_fn()

        langfuse.create_dataset_item(
            dataset_name=DATASET_NAME,
            input=input_data,
            expected_output=expected_output,
            metadata=metadata,
            id=f"{DATASET_NAME}_{name}",
        )
        print(f"  {PASS} Item '{name}' uploaded ({metadata.get('scenario', '')})")

    langfuse.flush()
    langfuse.shutdown()

    print(f"\n  {INFO} {len(ITEMS)} items uploaded to '{DATASET_NAME}'")
    print(f"  {INFO} View at: LangFuse → Datasets → {DATASET_NAME}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
