"""Tests for agent output guardrails."""

from __future__ import annotations

from datetime import UTC, datetime

from api.agent.guardrails import validate_action, validate_draft_recipients
from api.agent.models import (
    AgentResult,
    ClassificationResult,
    DraftEmail,
    EmailClassification,
    SuggestedAction,
)
from api.scheduling.models import (
    Coordinator,
    Loop,
    Stage,
    StageState,
)

NOW = datetime.now(UTC)


def _coordinator() -> Coordinator:
    return Coordinator(id="crd_1", name="Bob", email="bob@lrp.com", created_at=NOW)


def _loop(stage_state: StageState = StageState.NEW) -> Loop:
    return Loop(
        id="loop_1",
        coordinator_id="crd_1",
        client_contact_id="cli_1",
        recruiter_id="rec_1",
        candidate_id="cand_1",
        title="Jane Doe - Acme Corp",
        created_at=NOW,
        updated_at=NOW,
        stages=[
            Stage(
                id="stg_1",
                loop_id="loop_1",
                name="Round 1",
                state=stage_state,
                ordinal=0,
                created_at=NOW,
                updated_at=NOW,
            )
        ],
    )


def _classification(action: SuggestedAction) -> ClassificationResult:
    return ClassificationResult(
        classification=EmailClassification.NEW_INTERVIEW_REQUEST,
        suggested_action=action,
        confidence=0.9,
        reasoning="test",
    )


def _result(action: SuggestedAction) -> AgentResult:
    return AgentResult(classification=_classification(action))


class TestValidateAction:
    # --- create_loop ---
    def test_create_loop_valid_when_no_loop(self):
        violations = validate_action(_result(SuggestedAction.CREATE_LOOP), loop=None)
        assert violations == []

    def test_create_loop_invalid_when_loop_exists(self):
        violations = validate_action(_result(SuggestedAction.CREATE_LOOP), loop=_loop())
        assert len(violations) == 1
        assert "already exists" in violations[0]

    # --- draft_to_recruiter ---
    def test_draft_to_recruiter_valid_in_new(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_TO_RECRUITER),
            loop=_loop(StageState.NEW),
        )
        assert violations == []

    def test_draft_to_recruiter_invalid_in_awaiting_client(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_TO_RECRUITER),
            loop=_loop(StageState.AWAITING_CLIENT),
        )
        assert len(violations) == 1
        assert "draft_to_recruiter" in violations[0]

    # --- draft_to_client ---
    def test_draft_to_client_valid_in_awaiting_candidate(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_TO_CLIENT),
            loop=_loop(StageState.AWAITING_CANDIDATE),
        )
        assert violations == []

    def test_draft_to_client_invalid_in_new(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_TO_CLIENT),
            loop=_loop(StageState.NEW),
        )
        assert len(violations) == 1

    # --- draft_confirmation ---
    def test_draft_confirmation_valid_in_awaiting_client(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_CONFIRMATION),
            loop=_loop(StageState.AWAITING_CLIENT),
        )
        assert violations == []

    def test_draft_confirmation_invalid_in_new(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_CONFIRMATION),
            loop=_loop(StageState.NEW),
        )
        assert len(violations) == 1

    # --- draft_follow_up ---
    def test_draft_follow_up_valid_in_new(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_FOLLOW_UP),
            loop=_loop(StageState.NEW),
        )
        assert violations == []

    def test_draft_follow_up_valid_in_awaiting_candidate(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_FOLLOW_UP),
            loop=_loop(StageState.AWAITING_CANDIDATE),
        )
        assert violations == []

    def test_draft_follow_up_valid_in_awaiting_client(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_FOLLOW_UP),
            loop=_loop(StageState.AWAITING_CLIENT),
        )
        assert violations == []

    def test_draft_follow_up_invalid_in_scheduled(self):
        violations = validate_action(
            _result(SuggestedAction.DRAFT_FOLLOW_UP),
            loop=_loop(StageState.SCHEDULED),
        )
        assert len(violations) == 1

    # --- request_new_availability ---
    def test_request_new_availability_valid_in_awaiting_client(self):
        violations = validate_action(
            _result(SuggestedAction.REQUEST_NEW_AVAILABILITY),
            loop=_loop(StageState.AWAITING_CLIENT),
        )
        assert violations == []

    def test_request_new_availability_invalid_in_new(self):
        violations = validate_action(
            _result(SuggestedAction.REQUEST_NEW_AVAILABILITY),
            loop=_loop(StageState.NEW),
        )
        assert len(violations) == 1

    # --- mark_cold ---
    def test_mark_cold_valid_in_active_stages(self):
        for state in [
            StageState.NEW,
            StageState.AWAITING_CANDIDATE,
            StageState.AWAITING_CLIENT,
            StageState.SCHEDULED,
        ]:
            violations = validate_action(_result(SuggestedAction.MARK_COLD), loop=_loop(state))
            assert violations == [], f"Expected valid for state {state.value}"

    def test_mark_cold_invalid_in_complete(self):
        violations = validate_action(
            _result(SuggestedAction.MARK_COLD),
            loop=_loop(StageState.COMPLETE),
        )
        assert len(violations) == 1

    # --- no_action ---
    def test_no_action_valid_in_any_state(self):
        for state in StageState:
            violations = validate_action(_result(SuggestedAction.NO_ACTION), loop=_loop(state))
            assert violations == [], f"Expected valid for state {state.value}"

    def test_no_action_valid_without_loop(self):
        violations = validate_action(_result(SuggestedAction.NO_ACTION), loop=None)
        assert violations == []

    # --- ask_coordinator ---
    def test_ask_coordinator_valid_in_active_stages(self):
        for state in [
            StageState.NEW,
            StageState.AWAITING_CANDIDATE,
            StageState.AWAITING_CLIENT,
            StageState.SCHEDULED,
        ]:
            violations = validate_action(
                _result(SuggestedAction.ASK_COORDINATOR), loop=_loop(state)
            )
            assert violations == [], f"Expected valid for state {state.value}"

    # --- action requires loop but none exists ---
    def test_action_requires_loop_but_none(self):
        violations = validate_action(_result(SuggestedAction.DRAFT_TO_RECRUITER), loop=None)
        assert len(violations) == 1
        assert "requires an existing loop" in violations[0]


class TestValidateDraftRecipients:
    def test_all_known(self):
        draft = DraftEmail(
            to=["alice@example.com", "bob@example.com"],
            subject="Test",
            body="Hello",
        )
        unknown = validate_draft_recipients(draft, {"alice@example.com", "bob@example.com"})
        assert unknown == []

    def test_some_unknown(self):
        draft = DraftEmail(
            to=["alice@example.com", "stranger@evil.com"],
            subject="Test",
            body="Hello",
        )
        unknown = validate_draft_recipients(draft, {"alice@example.com", "bob@example.com"})
        assert unknown == ["stranger@evil.com"]

    def test_all_unknown(self):
        draft = DraftEmail(
            to=["x@example.com", "y@example.com"],
            subject="Test",
            body="Hello",
        )
        unknown = validate_draft_recipients(draft, {"z@example.com"})
        assert set(unknown) == {"x@example.com", "y@example.com"}

    def test_empty_recipients(self):
        draft = DraftEmail(to=[], subject="Test", body="Hello")
        unknown = validate_draft_recipients(draft, {"a@example.com"})
        assert unknown == []
