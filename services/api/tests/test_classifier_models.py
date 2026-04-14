"""Tests for classifier models and prompt builders."""

from datetime import UTC, datetime

from api.classifier.models import (
    ClassificationResult,
    EmailClassification,
    SuggestedAction,
    SuggestionItem,
    SuggestionStatus,
)
from api.classifier.prompts import (
    format_email,
    format_loop_state,
    format_stage_states,
    format_thread_history,
    format_transitions,
)
from api.gmail.models import EmailAddress, Message
from api.scheduling.models import (
    ALLOWED_TRANSITIONS,
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    Stage,
    StageState,
)


def _make_message(
    *,
    id: str = "msg_1",  # noqa: A002
    thread_id: str = "thread_1",
    subject: str = "Interview scheduling",
    from_email: str = "client@hedge.com",
    from_name: str | None = "Jane Client",
    to_email: str = "coordinator@lrp.com",
    body: str = "I'd like to interview John Smith.",
    date: datetime | None = None,
) -> Message:
    return Message(
        id=id,
        thread_id=thread_id,
        subject=subject,
        **{"from": EmailAddress(name=from_name, email=from_email)},
        to=[EmailAddress(name=None, email=to_email)],
        cc=[],
        date=date or datetime(2026, 4, 14, 10, 0, tzinfo=UTC),
        body_text=body,
    )


def _make_loop() -> Loop:
    return Loop(
        id="lop_test123",
        coordinator_id="crd_test",
        client_contact_id="cli_test",
        recruiter_id="con_test",
        candidate_id="can_test",
        title="John Smith - Acme Capital",
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 4, 14, tzinfo=UTC),
        coordinator=Coordinator(
            id="crd_test",
            name="Coord",
            email="coord@lrp.com",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        client_contact=ClientContact(
            id="cli_test",
            name="Jane Client",
            email="jane@hedge.com",
            company="Acme Capital",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        recruiter=Contact(
            id="con_test",
            name="Bob Recruiter",
            email="bob@recruit.com",
            role="recruiter",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        candidate=Candidate(
            id="can_test",
            name="John Smith",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        stages=[
            Stage(
                id="stg_test1",
                loop_id="lop_test123",
                name="Round 1",
                state=StageState.AWAITING_CANDIDATE,
                ordinal=0,
                created_at=datetime(2026, 4, 10, tzinfo=UTC),
                updated_at=datetime(2026, 4, 14, tzinfo=UTC),
            ),
        ],
    )


# --- Model tests ---


class TestClassificationResult:
    def test_parse_valid_single_suggestion(self):
        raw = {
            "suggestions": [
                {
                    "classification": "new_interview_request",
                    "action": "create_loop",
                    "confidence": 0.95,
                    "summary": "Client requests interview with John Smith",
                    "extracted_entities": {"candidate_name": "John Smith"},
                }
            ],
            "reasoning": "The email is a new scheduling request.",
        }
        result = ClassificationResult.model_validate(raw)
        assert len(result.suggestions) == 1
        assert result.suggestions[0].classification == EmailClassification.NEW_INTERVIEW_REQUEST
        assert result.suggestions[0].action == SuggestedAction.CREATE_LOOP
        assert result.suggestions[0].confidence == 0.95

    def test_parse_multiple_suggestions(self):
        raw = {
            "suggestions": [
                {
                    "classification": "time_confirmation",
                    "action": "advance_stage",
                    "confidence": 0.9,
                    "summary": "Client confirmed Tuesday at 2pm",
                    "target_state": "scheduled",
                },
                {
                    "classification": "new_interview_request",
                    "action": "create_loop",
                    "confidence": 0.8,
                    "summary": "Client also wants Round 2",
                },
            ],
            "reasoning": "Multi-action email.",
        }
        result = ClassificationResult.model_validate(raw)
        assert len(result.suggestions) == 2
        assert result.suggestions[0].target_state == StageState.SCHEDULED

    def test_not_scheduling(self):
        raw = {
            "suggestions": [
                {
                    "classification": "not_scheduling",
                    "action": "no_action",
                    "confidence": 0.98,
                    "summary": "Email about compensation, not scheduling",
                }
            ],
            "reasoning": "No scheduling content detected.",
        }
        result = ClassificationResult.model_validate(raw)
        assert result.suggestions[0].classification == EmailClassification.NOT_SCHEDULING
        assert result.suggestions[0].action == SuggestedAction.NO_ACTION

    def test_auto_advance_flag(self):
        item = SuggestionItem(
            classification=EmailClassification.AVAILABILITY_RESPONSE,
            action=SuggestedAction.ADVANCE_STAGE,
            confidence=0.9,
            summary="Coordinator forwarded availability",
            target_state=StageState.AWAITING_CLIENT,
            auto_advance=True,
        )
        assert item.auto_advance is True

    def test_suggestion_status_values(self):
        assert SuggestionStatus.PENDING == "pending"
        assert SuggestionStatus.SUPERSEDED == "superseded"
        assert SuggestionStatus.AUTO_APPLIED == "auto_applied"


# --- Prompt builder tests ---


class TestFormatEmail:
    def test_basic_format(self):
        msg = _make_message()
        text = format_email(msg)
        assert "Jane Client <client@hedge.com>" in text
        assert "coordinator@lrp.com" in text
        assert "Interview scheduling" in text
        assert "I'd like to interview John Smith." in text

    def test_no_name(self):
        msg = _make_message(from_name=None)
        text = format_email(msg)
        assert "client@hedge.com" in text
        assert "<" not in text.split("From:")[1].split("\n")[0]

    def test_empty_body(self):
        msg = _make_message(body="")
        text = format_email(msg)
        assert "(empty body)" in text


class TestFormatThreadHistory:
    def test_single_message(self):
        msg = _make_message()
        text = format_thread_history([msg])
        assert "Message 1" in text
        assert "Jane Client" in text

    def test_excludes_current_message(self):
        msg1 = _make_message(id="msg_1")
        msg2 = _make_message(id="msg_2", body="Reply here")
        text = format_thread_history([msg1, msg2], exclude_id="msg_2")
        assert "Reply here" not in text
        assert "I'd like to interview" in text

    def test_empty_thread(self):
        text = format_thread_history([])
        assert "no prior messages" in text

    def test_newest_first(self):
        old = _make_message(id="old", date=datetime(2026, 4, 10, tzinfo=UTC))
        new = _make_message(id="new", date=datetime(2026, 4, 14, tzinfo=UTC))
        text = format_thread_history([old, new])
        # Newest should appear first (Message 1)
        idx_new = text.index("Message 1")
        idx_old = text.index("Message 2")
        assert idx_new < idx_old


class TestFormatLoopState:
    def test_with_loop(self):
        loop = _make_loop()
        text = format_loop_state(loop)
        assert "John Smith - Acme Capital" in text
        assert "John Smith" in text
        assert "Acme Capital" in text
        assert "Bob Recruiter" in text
        assert "awaiting_candidate" in text

    def test_without_loop(self):
        text = format_loop_state(None)
        assert "No matching loop" in text


class TestFormatStageStates:
    def test_all_states_included(self):
        text = format_stage_states()
        for state in StageState:
            assert state.value in text


class TestFormatTransitions:
    def test_all_transitions_included(self):
        text = format_transitions()
        for from_state in ALLOWED_TRANSITIONS:
            assert from_state.value in text

    def test_new_to_awaiting_client_present(self):
        """Verify the NEW→AWAITING_CLIENT transition we added."""
        text = format_transitions()
        # The NEW line should contain awaiting_client
        for line in text.split("\n"):
            if line.strip().startswith("new →"):
                assert "awaiting_client" in line
                break
        else:
            raise AssertionError("NEW transition line not found")
