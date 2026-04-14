"""Tests for ClassifierHook guardrails."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from api.classifier.hook import ClassifierHook
from api.classifier.models import (
    EmailClassification,
    SuggestedAction,
    SuggestionItem,
)
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    Stage,
    StageState,
)


def _make_loop(stage_state: StageState = StageState.AWAITING_CANDIDATE) -> Loop:
    return Loop(
        id="lop_test",
        coordinator_id="crd_test",
        client_contact_id="cli_test",
        recruiter_id="con_test",
        candidate_id="can_test",
        title="Test Loop",
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 4, 14, tzinfo=UTC),
        stages=[
            Stage(
                id="stg_test",
                loop_id="lop_test",
                name="Round 1",
                state=stage_state,
                ordinal=0,
                created_at=datetime(2026, 4, 10, tzinfo=UTC),
                updated_at=datetime(2026, 4, 14, tzinfo=UTC),
            ),
        ],
        candidate=Candidate(
            id="can_test",
            name="John Smith",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        client_contact=ClientContact(
            id="cli_test",
            name="Jane",
            email="jane@hedge.com",
            company="Acme",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        recruiter=Contact(
            id="con_test",
            name="Bob",
            email="bob@rec.com",
            role="recruiter",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        coordinator=Coordinator(
            id="crd_test",
            name="Coord",
            email="coord@lrp.com",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


def _make_hook() -> ClassifierHook:
    return ClassifierHook(
        llm=MagicMock(),
        langfuse=MagicMock(),
        loop_service=AsyncMock(),
        suggestion_service=AsyncMock(),
        db_pool=MagicMock(),
    )


class TestGuardrails:
    def test_link_thread_low_confidence_demoted(self):
        """LINK_THREAD with confidence < 0.9 should be demoted to CREATE_LOOP."""
        hook = _make_hook()
        item = SuggestionItem(
            classification=EmailClassification.NEW_INTERVIEW_REQUEST,
            action=SuggestedAction.LINK_THREAD,
            confidence=0.7,
            summary="Might be related to existing loop",
            target_loop_id="lop_existing",
        )
        result = hook._apply_guardrails(item, _make_loop())
        assert result.action == SuggestedAction.CREATE_LOOP
        assert "confidence too low" in result.summary

    def test_link_thread_high_confidence_kept(self):
        """LINK_THREAD with confidence >= 0.9 should be kept."""
        hook = _make_hook()
        item = SuggestionItem(
            classification=EmailClassification.NEW_INTERVIEW_REQUEST,
            action=SuggestedAction.LINK_THREAD,
            confidence=0.95,
            summary="Matches existing loop",
            target_loop_id="lop_existing",
        )
        result = hook._apply_guardrails(item, _make_loop())
        assert result.action == SuggestedAction.LINK_THREAD

    def test_invalid_transition_demoted(self):
        """ADVANCE_STAGE with invalid transition should become ASK_COORDINATOR."""
        hook = _make_hook()
        loop = _make_loop(stage_state=StageState.AWAITING_CANDIDATE)
        # AWAITING_CANDIDATE → SCHEDULED is not allowed (must go through AWAITING_CLIENT)
        item = SuggestionItem(
            classification=EmailClassification.TIME_CONFIRMATION,
            action=SuggestedAction.ADVANCE_STAGE,
            confidence=0.9,
            summary="Client confirmed time",
            target_state=StageState.SCHEDULED,
        )
        result = hook._apply_guardrails(item, loop)
        assert result.action == SuggestedAction.ASK_COORDINATOR
        assert "not valid" in result.summary

    def test_valid_transition_kept(self):
        """ADVANCE_STAGE with valid transition should be kept."""
        hook = _make_hook()
        loop = _make_loop(stage_state=StageState.AWAITING_CANDIDATE)
        item = SuggestionItem(
            classification=EmailClassification.AVAILABILITY_RESPONSE,
            action=SuggestedAction.ADVANCE_STAGE,
            confidence=0.9,
            summary="Recruiter sent availability",
            target_state=StageState.AWAITING_CLIENT,
        )
        result = hook._apply_guardrails(item, loop)
        assert result.action == SuggestedAction.ADVANCE_STAGE
        assert result.target_state == StageState.AWAITING_CLIENT

    def test_new_to_awaiting_client_allowed(self):
        """NEW → AWAITING_CLIENT transition should be valid (client provides availability)."""
        hook = _make_hook()
        loop = _make_loop(stage_state=StageState.NEW)
        item = SuggestionItem(
            classification=EmailClassification.NEW_INTERVIEW_REQUEST,
            action=SuggestedAction.ADVANCE_STAGE,
            confidence=0.9,
            summary="Client provided availability upfront",
            target_state=StageState.AWAITING_CLIENT,
        )
        result = hook._apply_guardrails(item, loop)
        assert result.action == SuggestedAction.ADVANCE_STAGE
        assert result.target_state == StageState.AWAITING_CLIENT

    def test_no_action_passes_through(self):
        """NO_ACTION suggestions should pass through unchanged."""
        hook = _make_hook()
        item = SuggestionItem(
            classification=EmailClassification.NOT_SCHEDULING,
            action=SuggestedAction.NO_ACTION,
            confidence=0.98,
            summary="Not scheduling related",
        )
        result = hook._apply_guardrails(item, None)
        assert result.action == SuggestedAction.NO_ACTION
        assert result.confidence == 0.98
