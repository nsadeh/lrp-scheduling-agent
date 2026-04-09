"""Tests for the agent execution engine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from api.agent.engine import _extract_json, _parse_classification, _parse_draft, run_agent
from api.agent.llm import LLMResponse, LLMRouter
from api.agent.models import (
    AgentContext,
    ClassificationResult,
    DraftEmail,
    EmailClassification,
    SuggestedAction,
)
from api.gmail.models import EmailAddress, Message
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    LoopEvent,
)

# AgentContext uses TYPE_CHECKING forward refs; rebuild so Pydantic resolves them.
AgentContext.model_rebuild(
    _types_namespace={
        "Message": Message,
        "Loop": Loop,
        "LoopEvent": LoopEvent,
        "Coordinator": Coordinator,
        "Contact": Contact,
        "ClientContact": ClientContact,
        "Candidate": Candidate,
    }
)

NOW = datetime.now(UTC)


def _make_message(**overrides) -> Message:
    defaults = dict(
        id="msg_1",
        thread_id="thr_1",
        subject="Interview Request",
        **{"from": EmailAddress(name="Alice", email="alice@example.com")},
        to=[EmailAddress(name="Bob", email="bob@lrp.com")],
        date=NOW,
        body_text="Please schedule an interview for Jane Doe.",
    )
    defaults.update(overrides)
    return Message(**defaults)


def _make_coordinator() -> Coordinator:
    return Coordinator(id="crd_1", name="Bob Smith", email="bob@lrp.com", created_at=NOW)


def _make_context(**overrides) -> AgentContext:
    defaults = dict(
        new_message=_make_message(),
        thread_messages=[],
        coordinator=_make_coordinator(),
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


def _classification_json(**overrides) -> str:
    data = {
        "classification": "new_interview_request",
        "suggested_action": "create_loop",
        "confidence": 0.92,
        "reasoning": "Email asks to schedule an interview.",
        "questions": [],
        "prefilled_data": {"candidate_name": "Jane Doe"},
    }
    data.update(overrides)
    return json.dumps(data)


def _draft_json(**overrides) -> str:
    data = {
        "to": ["recruiter@example.com"],
        "subject": "Re: Interview Request",
        "body": "Hi Recruiter,\n\nCould you share availability?\n\nBest,\nBob",
        "in_reply_to": "<msg-id@example.com>",
    }
    data.update(overrides)
    return json.dumps(data)


class TestExtractJson:
    def test_plain_json(self):
        raw = '{"classification": "unrelated"}'
        assert _extract_json(raw) == '{"classification": "unrelated"}'

    def test_markdown_code_block(self):
        raw = '```json\n{"classification": "unrelated"}\n```'
        assert _extract_json(raw) == '{"classification": "unrelated"}'

    def test_markdown_code_block_no_lang(self):
        raw = '```\n{"classification": "unrelated"}\n```'
        assert _extract_json(raw) == '{"classification": "unrelated"}'

    def test_surrounding_text_ignored(self):
        raw = 'Here is my answer:\n```json\n{"key": "val"}\n```\nDone.'
        assert _extract_json(raw) == '{"key": "val"}'

    def test_strips_whitespace(self):
        raw = '   \n  {"a": 1}  \n  '
        assert _extract_json(raw) == '{"a": 1}'


class TestParseClassification:
    def test_clean_json(self):
        result = _parse_classification(_classification_json())
        assert isinstance(result, ClassificationResult)
        assert result.classification == EmailClassification.NEW_INTERVIEW_REQUEST
        assert result.suggested_action == SuggestedAction.CREATE_LOOP
        assert result.confidence == 0.92

    def test_markdown_wrapped_json(self):
        raw = f"```json\n{_classification_json()}\n```"
        result = _parse_classification(raw)
        assert result.classification == EmailClassification.NEW_INTERVIEW_REQUEST

    def test_with_questions(self):
        raw = _classification_json(questions=["Is this for Round 1 or Round 2?"])
        result = _parse_classification(raw)
        assert result.questions == ["Is this for Round 1 or Round 2?"]

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_classification("not json at all")


class TestParseDraft:
    def test_clean_json(self):
        result = _parse_draft(_draft_json())
        assert isinstance(result, DraftEmail)
        assert result.to == ["recruiter@example.com"]
        assert result.in_reply_to == "<msg-id@example.com>"

    def test_markdown_wrapped_json(self):
        raw = f"```json\n{_draft_json()}\n```"
        result = _parse_draft(raw)
        assert result.to == ["recruiter@example.com"]

    def test_null_in_reply_to_string(self):
        """LLM may emit the string 'null' instead of JSON null."""
        raw = _draft_json(in_reply_to="null")
        result = _parse_draft(raw)
        assert result.in_reply_to is None

    def test_null_in_reply_to_json(self):
        raw = _draft_json(in_reply_to=None)
        result = _parse_draft(raw)
        assert result.in_reply_to is None


class TestRunAgent:
    @pytest.mark.asyncio
    async def test_classify_only_for_non_draft_action(self):
        """When action does not require a draft, drafter is never called."""
        classify_resp = LLMResponse(
            content=_classification_json(),
            model="test",
            input_tokens=10,
            output_tokens=10,
            latency_ms=50.0,
        )
        classifier = AsyncMock(spec=LLMRouter)
        classifier.complete = AsyncMock(return_value=classify_resp)

        drafter = AsyncMock(spec=LLMRouter)
        drafter.complete = AsyncMock()

        ctx = _make_context()
        result = await run_agent(ctx, classifier, drafter)

        assert result.classification.suggested_action == SuggestedAction.CREATE_LOOP
        assert result.draft is None
        classifier.complete.assert_called_once()
        drafter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_classify_and_draft_for_draft_action(self):
        """When action requires a draft, both classifier and drafter are called."""
        classify_resp = LLMResponse(
            content=_classification_json(
                suggested_action="draft_to_recruiter",
                classification="new_interview_request",
            ),
            model="test",
            input_tokens=10,
            output_tokens=10,
            latency_ms=50.0,
        )
        draft_resp = LLMResponse(
            content=_draft_json(),
            model="test",
            input_tokens=20,
            output_tokens=30,
            latency_ms=100.0,
        )

        classifier = AsyncMock(spec=LLMRouter)
        classifier.complete = AsyncMock(return_value=classify_resp)

        drafter = AsyncMock(spec=LLMRouter)
        drafter.complete = AsyncMock(return_value=draft_resp)

        ctx = _make_context()
        result = await run_agent(ctx, classifier, drafter)

        assert result.classification.suggested_action == SuggestedAction.DRAFT_TO_RECRUITER
        assert result.draft is not None
        assert result.draft.to == ["recruiter@example.com"]
        classifier.complete.assert_called_once()
        drafter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_action_skips_draft(self):
        classify_resp = LLMResponse(
            content=_classification_json(
                suggested_action="no_action",
                classification="informational",
            ),
            model="test",
            input_tokens=10,
            output_tokens=10,
            latency_ms=50.0,
        )
        classifier = AsyncMock(spec=LLMRouter)
        classifier.complete = AsyncMock(return_value=classify_resp)

        drafter = AsyncMock(spec=LLMRouter)
        drafter.complete = AsyncMock()

        ctx = _make_context()
        result = await run_agent(ctx, classifier, drafter)

        assert result.classification.suggested_action == SuggestedAction.NO_ACTION
        assert result.draft is None
        drafter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_ask_coordinator_skips_draft(self):
        classify_resp = LLMResponse(
            content=_classification_json(
                suggested_action="ask_coordinator",
                classification="informational",
                questions=["Is this a first round?"],
            ),
            model="test",
            input_tokens=10,
            output_tokens=10,
            latency_ms=50.0,
        )
        classifier = AsyncMock(spec=LLMRouter)
        classifier.complete = AsyncMock(return_value=classify_resp)

        drafter = AsyncMock(spec=LLMRouter)
        drafter.complete = AsyncMock()

        ctx = _make_context()
        result = await run_agent(ctx, classifier, drafter)

        assert result.classification.suggested_action == SuggestedAction.ASK_COORDINATOR
        assert result.draft is None
        assert result.classification.questions == ["Is this a first round?"]

    @pytest.mark.asyncio
    async def test_all_draft_actions_trigger_drafter(self):
        """Every action in ACTIONS_REQUIRING_DRAFT should call drafter."""
        from api.agent.models import ACTIONS_REQUIRING_DRAFT

        for action in ACTIONS_REQUIRING_DRAFT:
            classify_resp = LLMResponse(
                content=_classification_json(suggested_action=action.value),
                model="test",
                input_tokens=10,
                output_tokens=10,
                latency_ms=50.0,
            )
            draft_resp = LLMResponse(
                content=_draft_json(),
                model="test",
                input_tokens=20,
                output_tokens=30,
                latency_ms=100.0,
            )

            classifier = AsyncMock(spec=LLMRouter)
            classifier.complete = AsyncMock(return_value=classify_resp)
            drafter = AsyncMock(spec=LLMRouter)
            drafter.complete = AsyncMock(return_value=draft_resp)

            ctx = _make_context()
            result = await run_agent(ctx, classifier, drafter)

            assert result.draft is not None, f"Draft missing for action {action.value}"
