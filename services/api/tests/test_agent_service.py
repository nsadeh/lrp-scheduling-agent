"""Tests for the AgentService suggestion CRUD."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.service import AgentService, Suggestion, SuggestionDraft


def _make_suggestion_row(
    *,
    id_="asg_test",
    loop_id="lop_test",
    stage_id="stg_test",
    gmail_message_id="msg_123",
    gmail_thread_id="thread_123",
    classification="new_interview_request",
    suggested_action="create_loop",
    questions=None,
    reasoning="Test reasoning",
    confidence=0.9,
    prefilled_data=None,
    status="pending",
    coordinator_feedback=None,
    created_at="2026-04-09T00:00:00Z",
    resolved_at=None,
    coordinator_email="nim@longridgepartners.com",
):
    return (
        id_,
        loop_id,
        stage_id,
        gmail_message_id,
        gmail_thread_id,
        classification,
        suggested_action,
        questions,
        reasoning,
        confidence,
        prefilled_data,
        status,
        coordinator_feedback,
        created_at,
        resolved_at,
        coordinator_email,
    )


def _make_draft_row(
    *,
    id_="sgd_test",
    suggestion_id="asg_test",
    draft_to=None,
    draft_subject="Re: Interview",
    draft_body="Hi, ...",
    in_reply_to=None,
    created_at="2026-04-09T00:00:00Z",
):
    return (
        id_,
        suggestion_id,
        draft_to or ["recruiter@example.com"],
        draft_subject,
        draft_body,
        in_reply_to,
        created_at,
    )


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.connection = MagicMock(return_value=ctx)
    return pool, conn


@pytest.fixture
def service(mock_pool):
    pool, _ = mock_pool
    return AgentService(db_pool=pool)


class TestSuggestion:
    def test_parses_row(self):
        row = _make_suggestion_row()
        s = Suggestion(row)
        assert s.id == "asg_test"
        assert s.classification == "new_interview_request"
        assert s.confidence == 0.9
        assert s.questions == []
        assert s.status == "pending"

    def test_parses_with_questions(self):
        row = _make_suggestion_row(questions=["Who is the candidate?"])
        s = Suggestion(row)
        assert s.questions == ["Who is the candidate?"]


class TestSuggestionDraft:
    def test_parses_row(self):
        row = _make_draft_row()
        d = SuggestionDraft(row)
        assert d.id == "sgd_test"
        assert d.draft_to == ["recruiter@example.com"]
        assert d.draft_subject == "Re: Interview"


class TestAgentService:
    async def test_create_suggestion(self, service, mock_pool):
        _, _conn = mock_pool
        expected_row = _make_suggestion_row()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "api.agent.service.queries.create_suggestion",
                AsyncMock(return_value=expected_row),
            )
            s = await service.create_suggestion(
                coordinator_email="nim@longridgepartners.com",
                loop_id="lop_test",
                stage_id="stg_test",
                gmail_message_id="msg_123",
                gmail_thread_id="thread_123",
                classification="new_interview_request",
                suggested_action="create_loop",
                confidence=0.9,
                reasoning="Test reasoning",
            )
        assert s.id == "asg_test"
        assert s.classification == "new_interview_request"

    async def test_create_draft(self, service, mock_pool):
        _, _conn = mock_pool
        expected_row = _make_draft_row()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "api.agent.service.queries.create_suggestion_draft",
                AsyncMock(return_value=expected_row),
            )
            d = await service.create_draft(
                suggestion_id="asg_test",
                draft_to=["recruiter@example.com"],
                draft_subject="Re: Interview",
                draft_body="Hi, ...",
            )
        assert d.id == "sgd_test"
        assert d.draft_to == ["recruiter@example.com"]

    async def test_get_latest_for_thread(self, service, mock_pool):
        _, _conn = mock_pool
        expected_row = _make_suggestion_row()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "api.agent.service.queries.get_latest_suggestion_for_thread",
                AsyncMock(return_value=expected_row),
            )
            s = await service.get_latest_for_thread("thread_123", "nim@longridgepartners.com")
        assert s is not None
        assert s.gmail_thread_id == "thread_123"

    async def test_get_latest_for_thread_none(self, service, mock_pool):
        _, _conn = mock_pool
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "api.agent.service.queries.get_latest_suggestion_for_thread",
                AsyncMock(return_value=None),
            )
            s = await service.get_latest_for_thread("unknown", "nim@longridgepartners.com")
        assert s is None

    async def test_resolve_suggestion(self, service, mock_pool):
        _, _conn = mock_pool
        with pytest.MonkeyPatch.context() as mp:
            mock_resolve = AsyncMock()
            mp.setattr("api.agent.service.queries.resolve_suggestion", mock_resolve)
            await service.resolve_suggestion("asg_test", status="accepted")
        mock_resolve.assert_called_once()
