"""Tests for agent domain models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from api.agent.models import (
    ACTIONS_REQUIRING_DRAFT,
    AgentResult,
    ClassificationResult,
    DraftEmail,
    EmailClassification,
    SuggestedAction,
    SuggestionStatus,
)

NOW = datetime.now(UTC)


class TestEmailClassification:
    def test_has_expected_values(self):
        expected = {
            "new_interview_request",
            "availability_response",
            "time_confirmation",
            "reschedule_request",
            "cancellation",
            "follow_up_needed",
            "informational",
            "unrelated",
        }
        assert {v.value for v in EmailClassification} == expected

    def test_is_str(self):
        assert isinstance(EmailClassification.NEW_INTERVIEW_REQUEST, str)
        assert EmailClassification.NEW_INTERVIEW_REQUEST == "new_interview_request"


class TestSuggestedAction:
    def test_has_expected_values(self):
        expected = {
            "draft_to_recruiter",
            "draft_to_client",
            "draft_confirmation",
            "draft_follow_up",
            "request_new_availability",
            "mark_cold",
            "create_loop",
            "ask_coordinator",
            "no_action",
        }
        assert {v.value for v in SuggestedAction} == expected


class TestActionsRequiringDraft:
    def test_correct_actions(self):
        expected = {
            SuggestedAction.DRAFT_TO_RECRUITER,
            SuggestedAction.DRAFT_TO_CLIENT,
            SuggestedAction.DRAFT_CONFIRMATION,
            SuggestedAction.DRAFT_FOLLOW_UP,
            SuggestedAction.REQUEST_NEW_AVAILABILITY,
        }
        assert expected == ACTIONS_REQUIRING_DRAFT

    def test_non_draft_actions_excluded(self):
        non_draft = {
            SuggestedAction.MARK_COLD,
            SuggestedAction.CREATE_LOOP,
            SuggestedAction.ASK_COORDINATOR,
            SuggestedAction.NO_ACTION,
        }
        assert (ACTIONS_REQUIRING_DRAFT & non_draft) == set()


class TestSuggestionStatus:
    def test_has_expected_values(self):
        expected = {"pending", "accepted", "edited", "rejected"}
        assert {v.value for v in SuggestionStatus} == expected


class TestClassificationResult:
    def _result(self, **overrides) -> ClassificationResult:
        defaults = {
            "classification": EmailClassification.NEW_INTERVIEW_REQUEST,
            "suggested_action": SuggestedAction.CREATE_LOOP,
            "confidence": 0.95,
            "reasoning": "Contains interview scheduling language",
        }
        defaults.update(overrides)
        return ClassificationResult(**defaults)

    def test_valid_result(self):
        r = self._result()
        assert r.classification == EmailClassification.NEW_INTERVIEW_REQUEST
        assert r.confidence == 0.95

    def test_confidence_bounds_low(self):
        r = self._result(confidence=0.0)
        assert r.confidence == 0.0

    def test_confidence_bounds_high(self):
        r = self._result(confidence=1.0)
        assert r.confidence == 1.0

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            self._result(confidence=-0.1)

    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValidationError):
            self._result(confidence=1.1)

    def test_questions_default_empty(self):
        r = self._result()
        assert r.questions == []

    def test_prefilled_data_default_none(self):
        r = self._result()
        assert r.prefilled_data is None

    def test_prefilled_data_accepts_dict(self):
        r = self._result(prefilled_data={"candidate_name": "Jane"})
        assert r.prefilled_data == {"candidate_name": "Jane"}


class TestDraftEmail:
    def test_valid_draft(self):
        d = DraftEmail(
            to=["recruiter@example.com"],
            subject="Interview availability",
            body="Hi, please provide availability.",
        )
        assert d.to == ["recruiter@example.com"]
        assert d.in_reply_to is None

    def test_with_in_reply_to(self):
        d = DraftEmail(
            to=["recruiter@example.com"],
            subject="Re: Interview",
            body="Thanks for the times.",
            in_reply_to="<msg-id@example.com>",
        )
        assert d.in_reply_to == "<msg-id@example.com>"

    def test_multiple_recipients(self):
        d = DraftEmail(
            to=["a@example.com", "b@example.com"],
            subject="Test",
            body="Body",
        )
        assert len(d.to) == 2


class TestAgentResult:
    def _classification(self) -> ClassificationResult:
        return ClassificationResult(
            classification=EmailClassification.AVAILABILITY_RESPONSE,
            suggested_action=SuggestedAction.DRAFT_TO_CLIENT,
            confidence=0.88,
            reasoning="Candidate provided time slots",
        )

    def test_without_draft(self):
        result = AgentResult(classification=self._classification())
        assert result.draft is None

    def test_with_draft(self):
        draft = DraftEmail(
            to=["client@example.com"],
            subject="Available times",
            body="Here are the candidate's available times.",
        )
        result = AgentResult(classification=self._classification(), draft=draft)
        assert result.draft is not None
        assert result.draft.to == ["client@example.com"]

    def test_classification_accessible(self):
        result = AgentResult(classification=self._classification())
        assert result.classification.suggested_action == SuggestedAction.DRAFT_TO_CLIENT
