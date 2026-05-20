"""Tests for the two-stage classification pipeline: Router, LoopClassifier, NextActionAgent."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.classifier.loop_classifier import LoopClassifier, _resolve_coordinator_name
from api.classifier.models import (
    ClassificationResult,
    EmailClassification,
    SuggestedAction,
    Suggestion,
    SuggestionItem,
    SuggestionStatus,
)
from api.classifier.next_action_agent import NextActionAgent
from api.classifier.router import EmailRouter, _coordinator_on_trigger, _is_internal_only
from api.classifier.sender_blacklist import SenderBlacklist
from api.gmail.hooks import EmailEvent, MessageDirection, MessageType
from api.gmail.models import EmailAddress, Message
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    StageState,
)


def _msg(msg_id="msg1", thread_id="thread1", from_email="alice@example.com") -> Message:
    return Message(
        id=msg_id,
        thread_id=thread_id,
        subject="Interview",
        **{"from": EmailAddress(name="Alice", email=from_email)},
        to=[EmailAddress(email="coord@lrp.com")],
        date=datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
        body_text="Hello world",
    )


def _event(
    direction=MessageDirection.INCOMING,
    msg_id="msg1",
    thread_id="thread1",
    from_email="alice@example.com",
) -> EmailEvent:
    return EmailEvent(
        message=_msg(msg_id, thread_id, from_email=from_email),
        coordinator_email="coord@lrp.com",
        direction=direction,
        message_type=MessageType.REPLY,
        new_participants=[],
    )


def _loop(loop_id="lop_1", state=StageState.AWAITING_CANDIDATE) -> Loop:
    return Loop(
        id=loop_id,
        coordinator_id="crd_1",
        client_contact_id="cli_1",
        recruiter_id="con_1",
        candidate_id="can_1",
        title="Round 1 - John Smith",
        state=state,
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 4, 14, tzinfo=UTC),
        candidate=Candidate(
            id="can_1", name="John Smith", created_at=datetime(2026, 4, 10, tzinfo=UTC)
        ),
        client_contact=ClientContact(
            id="cli_1",
            name="Jane",
            email="jane@hf.com",
            company="HF Co",
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
        ),
        recruiter=Contact(
            id="con_1",
            name="Bob",
            email="bob@lrp.com",
            role="recruiter",
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
        ),
    )


def _suggestion_item(
    classification=EmailClassification.AVAILABILITY_RESPONSE,
    action=SuggestedAction.ADVANCE_STAGE,
    confidence=0.95,
    target_loop_id="lop_1",
    target_stage=StageState.AWAITING_CLIENT,
    action_data=None,
) -> SuggestionItem:
    if action_data is None:
        if action == SuggestedAction.ADVANCE_STAGE:
            action_data = {"target_stage": target_stage.value}
        elif action == SuggestedAction.DRAFT_EMAIL:
            action_data = {"body": "Draft something", "recipient_type": "recruiter"}
        elif action == SuggestedAction.ASK_COORDINATOR:
            action_data = {"question": "What should I do?"}
        elif action == SuggestedAction.LINK_THREAD:
            action_data = {"candidate_name": "Test Candidate", "client_company": "Test Company"}
        elif action == SuggestedAction.CREATE_LOOP:
            action_data = {"candidate_name": "Test Candidate"}
        else:
            action_data = {}
    return SuggestionItem(
        classification=classification,
        action=action,
        confidence=confidence,
        summary="Test suggestion",
        reasoning="Test reasoning",
        target_loop_id=target_loop_id,
        action_data=action_data,
    )


def _classification_result(items=None, reasoning="test"):
    return ClassificationResult(
        suggestions=items or [_suggestion_item()],
        reasoning=reasoning,
    )


def _make_router(sender_blacklist: SenderBlacklist | None = None):
    """Create an EmailRouter with mocked classifier and agent."""
    classifier = MagicMock(spec=LoopClassifier)
    classifier.classify = AsyncMock()

    agent = MagicMock(spec=NextActionAgent)
    agent.act = AsyncMock()

    loop_service = MagicMock()
    loop_service.find_loops_by_thread = AsyncMock(return_value=[])

    router = EmailRouter(
        loop_classifier=classifier,
        next_action_agent=agent,
        loop_service=loop_service,
        sender_blacklist=sender_blacklist,
    )
    return router, classifier, agent, loop_service


def _make_classifier():
    """Create a LoopClassifier with mocked dependencies."""
    llm = MagicMock()
    langfuse = MagicMock()
    suggestion_service = MagicMock()
    suggestion_service.create_suggestion = AsyncMock(return_value=MagicMock(id="sug_test"))

    loop_service = MagicMock()
    loop_service.get_coordinator_by_email = AsyncMock(return_value=None)
    loop_service._pool = MagicMock()

    classifier = LoopClassifier(
        llm=llm,
        langfuse=langfuse,
        suggestion_service=suggestion_service,
        loop_service=loop_service,
    )
    return classifier, suggestion_service


def _make_agent():
    """Create a NextActionAgent with mocked dependencies."""
    llm = MagicMock()
    langfuse = MagicMock()
    suggestion_service = MagicMock()
    suggestion_service.create_suggestion = AsyncMock(return_value=MagicMock(id="sug_test"))
    suggestion_service.get_pending_for_loop = AsyncMock(return_value=[])

    loop_service = MagicMock()
    loop_service.get_coordinator_by_email = AsyncMock(return_value=None)
    loop_service.get_events = AsyncMock(return_value=[])

    agent = NextActionAgent(
        llm=llm,
        langfuse=langfuse,
        suggestion_service=suggestion_service,
        loop_service=loop_service,
    )
    return agent, suggestion_service


def _llm_response(content: str, *, finish_reason: str = "stop", completion_tokens: int = 100):
    """Build a real `LLMResponse` (not a MagicMock) for tests.

    The agents call `response.to_diagnostics(...)` which is a real method on
    the `LLMResponse` dataclass — a MagicMock would silently return a child
    Mock instead of the diagnostic dict, masking failures. Default values
    mirror the most common real-world response shape so tests touching
    diagnostics don't have to set them every time.
    """
    from api.ai.llm_service import LLMResponse

    return LLMResponse(
        content=content,
        model="test-model",
        provider="test",
        finish_reason=finish_reason,
        latency_ms=1.0,
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": completion_tokens,
            "total_tokens": 1000 + completion_tokens,
        },
    )


def _suggestions_envelope(items: list[SuggestionItem]) -> str:
    """Re-serialize SuggestionItems into the prompt's <suggestions>[...]</suggestions> envelope."""
    payload = [item.model_dump(mode="json") for item in items]
    return f"<suggestions>{json.dumps(payload)}</suggestions>"


class _FakeTextPrompt:
    """Stands in for a LangFuse TextPromptClient — falls through the non-chat
    branch in ``NextActionAgent._build_initial_messages`` so tests don't have
    to model the chat compile contract.
    """

    from typing import ClassVar

    version = 1
    labels: ClassVar[tuple[str, ...]] = ("test",)
    config: ClassVar[dict] = {
        "model": "test-model",
        "temperature": 0.0,
        "max_tokens": 1024,
    }

    def compile(self, **_kwargs):
        return "compiled prompt"


def _patch_agent_llm(agent, content: str):
    """Patch the agent's prompt fetch + LLM call to return ``content``."""
    agent._llm.complete = AsyncMock(return_value=_llm_response(content))
    return patch(
        "api.classifier.next_action_agent.fetch_prompt",
        return_value=_FakeTextPrompt(),
    )


# --- Router tests ---


class TestRouterRouting:
    @pytest.mark.asyncio
    async def test_outgoing_on_unlinked_thread_skips(self):
        router, classifier, agent, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []

        event = _event(direction=MessageDirection.OUTGOING)
        await router.on_email(event)

        classifier.classify.assert_not_called()
        agent.act.assert_not_called()

    @pytest.mark.asyncio
    async def test_outgoing_on_linked_thread_routes_to_agent(self):
        router, classifier, agent, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = [_loop()]

        event = _event(direction=MessageDirection.OUTGOING)
        await router.on_email(event)

        agent.act.assert_called_once()
        classifier.classify.assert_not_called()

    @pytest.mark.asyncio
    async def test_incoming_on_unlinked_thread_routes_to_classifier(self):
        router, classifier, agent, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []

        event = _event(direction=MessageDirection.INCOMING)
        await router.on_email(event)

        classifier.classify.assert_called_once()
        agent.act.assert_not_called()

    @pytest.mark.asyncio
    async def test_incoming_on_linked_thread_routes_to_agent(self):
        router, classifier, agent, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = [_loop()]

        event = _event(direction=MessageDirection.INCOMING)
        await router.on_email(event)

        agent.act.assert_called_once()
        classifier.classify.assert_not_called()


class TestSenderBlacklist:
    @pytest.mark.asyncio
    async def test_blacklisted_sender_skips(self):
        blacklist = SenderBlacklist(domains=frozenset({"withintelligence-email.com"}))
        router, classifier, agent, loop_service = _make_router(sender_blacklist=blacklist)

        event = _event(from_email="alerts@withintelligence-email.com")
        await router.on_email(event)

        classifier.classify.assert_not_called()
        agent.act.assert_not_called()
        loop_service.find_loops_by_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_blacklisted_sender_routes_normally(self):
        blacklist = SenderBlacklist(domains=frozenset({"withintelligence-email.com"}))
        router, classifier, _, loop_service = _make_router(sender_blacklist=blacklist)
        loop_service.find_loops_by_thread.return_value = []

        event = _event(from_email="alice@candidate.com")
        await router.on_email(event)

        classifier.classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_blacklist_uses_empty_default(self):
        router, classifier, _, loop_service = _make_router(sender_blacklist=None)
        loop_service.find_loops_by_thread.return_value = []

        event = _event(from_email="alerts@withintelligence-email.com")
        await router.on_email(event)

        classifier.classify.assert_called_once()


# --- Classifier guardrails ---


class TestClassifierGuardrails:
    def test_link_thread_below_threshold_converts_to_create_loop(self):
        classifier, _ = _make_classifier()
        item = _suggestion_item(action=SuggestedAction.LINK_THREAD, confidence=0.8)
        result, error = classifier._apply_guardrails(item)
        assert result.action == SuggestedAction.CREATE_LOOP
        assert error is not None
        assert "confidence" in error

    def test_link_thread_above_threshold_passes(self):
        classifier, _ = _make_classifier()
        item = _suggestion_item(action=SuggestedAction.LINK_THREAD, confidence=0.95)
        result, error = classifier._apply_guardrails(item)
        assert result.action == SuggestedAction.LINK_THREAD
        assert error is None

    def test_disallowed_action_converts_to_no_action(self):
        classifier, _ = _make_classifier()
        item = _suggestion_item(action=SuggestedAction.ADVANCE_STAGE)
        result, error = classifier._apply_guardrails(item)
        assert result.action == SuggestedAction.NO_ACTION
        assert error is not None

    # ---- CREATE_LOOP: client-manager allow-list ----

    def _create_loop_item(self, **overrides):
        """Helper: well-formed CREATE_LOOP with all fields populated."""
        action_data = {
            "candidate_name": "Jordan Martinez",
            "client_name": "David Chen",
            "client_email": "dchen@apexcap.com",
            "client_company": "Apex Capital",
            "cm_name": "Adam L'esperance",
            "cm_email": "adam@longridgepartners.com",
        }
        action_data.update(overrides)
        return _suggestion_item(action=SuggestedAction.CREATE_LOOP, action_data=action_data)

    def test_create_loop_with_valid_cm_passes(self):
        classifier, _ = _make_classifier()
        item = self._create_loop_item()
        _result, error = classifier._apply_guardrails(item)
        assert error is None

    def test_create_loop_with_invalid_cm_email_errors(self):
        classifier, _ = _make_classifier()
        item = self._create_loop_item(cm_email="someone-else@longridgepartners.com")
        _result, error = classifier._apply_guardrails(item)
        assert error is not None
        assert "client-manager" in error.lower()

    def test_create_loop_cm_email_is_case_insensitive(self):
        classifier, _ = _make_classifier()
        # Model sometimes returns mixed-case (e.g. RQuatroni) — should still pass.
        item = self._create_loop_item(cm_email="RQuatroni@longridgepartners.com")
        _result, error = classifier._apply_guardrails(item)
        assert error is None

    # ---- CREATE_LOOP: client-contact validity ----

    def test_create_loop_client_same_as_candidate_errors(self):
        classifier, _ = _make_classifier()
        item = self._create_loop_item(
            candidate_name="Jordan Martinez", client_name="jordan martinez"
        )
        _result, error = classifier._apply_guardrails(item)
        assert error is not None
        assert "candidate" in error.lower()

    def test_create_loop_client_email_lrp_domain_errors(self):
        classifier, _ = _make_classifier()
        item = self._create_loop_item(client_email="someone@longridgepartners.com")
        _result, error = classifier._apply_guardrails(item)
        assert error is not None
        assert "longridgepartners" in error

    def test_create_loop_client_email_gmail_errors(self):
        classifier, _ = _make_classifier()
        item = self._create_loop_item(client_email="dchen@gmail.com")
        _result, error = classifier._apply_guardrails(item)
        assert error is not None
        assert "consumer" in error.lower()

    def test_create_loop_client_email_hotmail_errors(self):
        classifier, _ = _make_classifier()
        item = self._create_loop_item(client_email="contact@hotmail.com")
        _result, error = classifier._apply_guardrails(item)
        assert error is not None
        assert "consumer" in error.lower()

    # ---- CREATE_LOOP: client info optional (ATS path) ----

    def test_create_loop_missing_client_email_is_allowed(self):
        """Automated ATS emails (Greenhouse, etc.) may have no extractable client
        contact — the CM/recruiter is on the thread but the named client isn't.
        Loop creation should proceed; the coordinator fills client details later.
        """
        classifier, _ = _make_classifier()
        item = self._create_loop_item(client_email="", client_name="")
        _result, error = classifier._apply_guardrails(item)
        assert error is None

    def test_create_loop_missing_client_email_with_name_present_is_allowed(self):
        """client_name set but client_email empty — still OK (name without contact)."""
        classifier, _ = _make_classifier()
        item = self._create_loop_item(client_email="", client_name="Olivia Chen")
        _result, error = classifier._apply_guardrails(item)
        assert error is None

    def test_create_loop_malformed_client_email_errors(self):
        """A non-empty but malformed email (no @) is still an error — we never
        want a partial/broken email recorded; the model should omit instead.
        """
        classifier, _ = _make_classifier()
        item = self._create_loop_item(client_email="not-an-email-address")
        _result, error = classifier._apply_guardrails(item)
        assert error is not None
        assert "malformed" in error.lower()


# --- Agent guardrails ---


class TestAgentGuardrails:
    def test_create_loop_blacklisted(self):
        agent, _ = _make_agent()
        item = _suggestion_item(action=SuggestedAction.CREATE_LOOP)
        _result, error = agent._apply_guardrails(item, set())
        assert error is not None
        assert "not allowed" in error

    def test_link_thread_blacklisted(self):
        agent, _ = _make_agent()
        item = _suggestion_item(action=SuggestedAction.LINK_THREAD)
        _result, error = agent._apply_guardrails(item, set())
        assert error is not None

    def test_advance_stage_passes(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.ADVANCE_STAGE,
            target_stage=StageState.AWAITING_CLIENT,
        )
        result, error = agent._apply_guardrails(item, set())
        assert result.action == SuggestedAction.ADVANCE_STAGE
        assert error is None

    def test_draft_email_passes(self):
        agent, _ = _make_agent()
        item = _suggestion_item(action=SuggestedAction.DRAFT_EMAIL)
        result, error = agent._apply_guardrails(item, set())
        assert result.action == SuggestedAction.DRAFT_EMAIL
        assert error is None

    def test_draft_email_placeholder_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.DRAFT_EMAIL,
            action_data={
                "body": "Hi [Recruiter name], here are the times.",
                "recipient_type": "recruiter",
            },
        )
        _result, error = agent._apply_guardrails(item, set())
        assert error is not None
        assert "placeholder" in error.lower()

    def test_draft_email_bracketed_token_anywhere_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.DRAFT_EMAIL,
            action_data={
                "body": "Confirming the [DATE] slot works.",
                "recipient_type": "recruiter",
            },
        )
        _result, error = agent._apply_guardrails(item, set())
        assert error is not None
        assert "placeholder" in error.lower()

    def test_draft_email_clean_body_passes(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.DRAFT_EMAIL,
            action_data={
                "body": "Hi Sarah, can you confirm Tuesday 2pm ET?",
                "recipient_type": "recruiter",
            },
        )
        result, error = agent._apply_guardrails(item, set())
        assert error is None
        assert result.action == SuggestedAction.DRAFT_EMAIL

    def test_missing_target_loop_id_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(action=SuggestedAction.DRAFT_EMAIL, target_loop_id=None)
        _result, error = agent._apply_guardrails(item, set())
        assert error is not None
        assert "target_loop_id" in error

    def test_invalid_action_data_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.ADVANCE_STAGE,
            action_data={},  # missing target_stage
        )
        _result, error = agent._apply_guardrails(item, set())
        assert error is not None
        assert "action_data" in error

    def test_expire_suggestion_unknown_id_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.EXPIRE_SUGGESTION,
            action_data={"suggestion_id": "sug_unknown"},
        )
        _result, error = agent._apply_guardrails(item, {"sug_other"})
        assert error is not None
        assert "expire_suggestion" in error

    def test_expire_suggestion_known_id_passes(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.EXPIRE_SUGGESTION,
            action_data={"suggestion_id": "sug_stale"},
        )
        result, error = agent._apply_guardrails(item, {"sug_stale"})
        assert error is None
        assert result.action == SuggestedAction.EXPIRE_SUGGESTION

    def test_update_actor_valid_role_passes(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "recruiter"},
        )
        result, error = agent._apply_guardrails(item, set())
        assert error is None
        assert result.action == SuggestedAction.UPDATE_ACTOR

    def test_update_actor_invalid_role_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.UPDATE_ACTOR,
            action_data={"role": "wizard"},  # not in the Literal
        )
        _result, error = agent._apply_guardrails(item, set())
        assert error is not None
        assert "action_data" in error


# --- Error handling ---


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_classifier_llm_failure_creates_needs_attention(self):
        classifier, suggestion_service = _make_classifier()
        classifier._llm.complete = AsyncMock(side_effect=Exception("LLM down"))

        with patch(
            "api.classifier.loop_classifier.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            event = _event()
            await classifier.classify(event)

        suggestion_service.create_suggestion.assert_called_once()
        call_kwargs = suggestion_service.create_suggestion.call_args.kwargs
        assert call_kwargs["item"].action == SuggestedAction.ASK_COORDINATOR
        assert call_kwargs["item"].confidence == 0.0

    @pytest.mark.asyncio
    async def test_agent_llm_failure_creates_needs_attention(self):
        agent, suggestion_service = _make_agent()
        agent._llm.complete = AsyncMock(side_effect=Exception("LLM down"))

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            event = _event()
            await agent.act(event, [_loop()])

        suggestion_service.create_suggestion.assert_called_once()
        call_kwargs = suggestion_service.create_suggestion.call_args.kwargs
        assert call_kwargs["item"].action == SuggestedAction.ASK_COORDINATOR
        assert call_kwargs["item"].confidence == 0.0

    @pytest.mark.asyncio
    async def test_parse_failure_writes_raw_responses_to_langfuse(self):
        """When both the initial LLM call and the retry produce un-parseable
        content (e.g., classification not in the enum), the agent must put
        the raw response strings on the LangFuse span output before the
        manual-review fallback fires. Without this, the trace shows an
        empty output and we can't see what the model actually produced.
        """
        agent, suggestion_service = _make_agent()
        # Both responses are valid JSON but the classification is invalid —
        # mirrors the real failure case the user reported.
        bad_envelope = (
            "<suggestions>[{"
            '"classification": "information",'  # not in the enum
            '"action": "ask_coordinator",'
            '"confidence": 0.5,'
            '"summary": "x",'
            '"reasoning": "x",'
            '"target_loop_id": "lop_1",'
            '"action_data": {"question": "x"}'
            "}]</suggestions>"
        )
        agent._llm.complete = AsyncMock(
            side_effect=[
                _llm_response(bad_envelope),
                _llm_response(bad_envelope),  # retry also fails
            ]
        )

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(_event(), [_loop()])

        # Find the span update that carries raw_responses + parse_error.
        update_calls = agent._langfuse.update_current_span.call_args_list
        failure_update = next(
            (call for call in update_calls if "parse_error" in (call.kwargs.get("output") or {})),
            None,
        )
        assert failure_update is not None, (
            "expected update_current_span(output={...'parse_error'...}) "
            "before the fallback was created"
        )
        output = failure_update.kwargs["output"]
        assert output["raw_responses"] == [bad_envelope, bad_envelope]
        parse_error = output["parse_error"].lower()
        assert "validation" in parse_error or "schema" in parse_error

        # Manual-review fallback still fires (no regression on that behavior).
        suggestion_service.create_suggestion.assert_called_once()
        call_kwargs = suggestion_service.create_suggestion.call_args.kwargs
        assert call_kwargs["item"].action == SuggestedAction.ASK_COORDINATOR
        assert call_kwargs["item"].confidence == 0.0

    @pytest.mark.asyncio
    async def test_parse_failure_writes_call_diagnostics_to_langfuse(self):
        """LangFuse span output must include per-call diagnostics
        (finish_reason, completion_tokens, model, provider) on the failure
        path. Without these, "agent failed to parse" traces are
        indistinguishable from each other and can't be triaged from the
        dashboard alone.
        """
        agent, _ = _make_agent()
        bad = "no envelope in here at all"
        agent._llm.complete = AsyncMock(
            side_effect=[
                _llm_response(bad, finish_reason="stop", completion_tokens=42),
                _llm_response(bad, finish_reason="stop", completion_tokens=18),
            ]
        )

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(_event(), [_loop()])

        update_calls = agent._langfuse.update_current_span.call_args_list
        failure_update = next(
            (call for call in update_calls if "parse_error" in (call.kwargs.get("output") or {})),
            None,
        )
        assert failure_update is not None
        output = failure_update.kwargs["output"]
        diagnostics = output["call_diagnostics"]
        assert len(diagnostics) == 2, "expected one diagnostics entry per LLM call"

        # First attempt diagnostics
        assert diagnostics[0]["attempt"] == 0
        assert diagnostics[0]["finish_reason"] == "stop"
        assert diagnostics[0]["completion_tokens"] == 42
        assert diagnostics[0]["model"] == "test-model"
        assert diagnostics[0]["provider"] == "test"
        assert diagnostics[0]["content_length_chars"] == len(bad)

        # Retry attempt diagnostics
        assert diagnostics[1]["attempt"] == 1
        assert diagnostics[1]["completion_tokens"] == 18

    def test_next_action_agent_error_carries_raw_responses(self):
        """Plain unit assertion that the exception preserves raw_responses
        and call_diagnostics."""
        from api.classifier.next_action_agent import NextActionAgentError

        diag = [{"attempt": 0, "finish_reason": "stop", "completion_tokens": 100}]
        exc = NextActionAgentError("nope", raw_responses=["a", "b"], call_diagnostics=diag)
        assert exc.raw_responses == ["a", "b"]
        assert exc.call_diagnostics == diag
        # Defaults empty when not provided.
        bare = NextActionAgentError("nope")
        assert bare.raw_responses == []
        assert bare.call_diagnostics == []


# --- Coordinator name resolution ---


class TestResolveCoordinatorName:
    """Layered fallback: DB row → Gmail header display name → email local-part."""

    def _coord(self, name: str) -> Coordinator:
        return Coordinator(
            id="crd_1",
            name=name,
            email="coord@lrp.com",
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
        )

    def test_uses_db_coordinator_name_when_present(self):
        event = _event()
        name = _resolve_coordinator_name(event, self._coord("Nim Sadeh"))
        assert name == "Nim Sadeh"

    def test_falls_back_to_incoming_to_header_display_name(self):
        msg = Message(
            id="msg1",
            thread_id="thread1",
            subject="Interview",
            **{"from": EmailAddress(name="Alice", email="alice@example.com")},
            to=[EmailAddress(name="Nim (from Gmail)", email="coord@lrp.com")],
            date=datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
            body_text="Hello",
        )
        event = EmailEvent(
            message=msg,
            coordinator_email="coord@lrp.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        assert _resolve_coordinator_name(event, None) == "Nim (from Gmail)"

    def test_falls_back_to_outgoing_from_header_display_name(self):
        msg = Message(
            id="msg1",
            thread_id="thread1",
            subject="Interview",
            **{"from": EmailAddress(name="Nim Sadeh", email="coord@lrp.com")},
            to=[EmailAddress(email="alice@example.com")],
            date=datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
            body_text="Hello",
        )
        event = EmailEvent(
            message=msg,
            coordinator_email="coord@lrp.com",
            direction=MessageDirection.OUTGOING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        assert _resolve_coordinator_name(event, None) == "Nim Sadeh"

    def test_falls_back_to_local_part_when_no_display_name_anywhere(self):
        event = _event()
        assert _resolve_coordinator_name(event, None) == "coord"


# --- Internal-only filter ---


def _internal_msg(
    from_email="alice@longridgepartners.com",
    to_emails=("bob@longridgepartners.com",),
    cc_emails=(),
) -> Message:
    return Message(
        id="msg1",
        thread_id="thread1",
        subject="Internal",
        **{"from": EmailAddress(email=from_email)},
        to=[EmailAddress(email=e) for e in to_emails],
        cc=[EmailAddress(email=e) for e in cc_emails],
        date=datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
        body_text="Hey",
    )


class TestIsInternalOnly:
    def test_all_internal(self):
        msg = _internal_msg(cc_emails=("carol@longridgepartners.com",))
        assert _is_internal_only(msg) is True

    def test_external_in_to(self):
        msg = _internal_msg(to_emails=("ext@gmail.com",))
        assert _is_internal_only(msg) is False

    def test_external_in_cc(self):
        msg = _internal_msg(cc_emails=("ext@other.com",))
        assert _is_internal_only(msg) is False

    def test_external_from(self):
        msg = _internal_msg(from_email="ext@candidate.com")
        assert _is_internal_only(msg) is False

    def test_case_insensitive(self):
        msg = _internal_msg(
            from_email="Alice@LongRidgePartners.COM",
            to_emails=("BOB@LONGRIDGEPARTNERS.COM",),
        )
        assert _is_internal_only(msg) is True

    def test_empty_to_and_cc(self):
        msg = _internal_msg(to_emails=(), cc_emails=())
        assert _is_internal_only(msg) is True

    def test_thread_with_external_upstream_is_not_internal_only(self):
        """If any message in the thread had an external participant, the
        thread isn't internal-only even when the trigger message is all-LRP."""
        external_inbound = Message(
            id="msg0",
            thread_id="thread1",
            subject="Interview request",
            **{"from": EmailAddress(email="client@external.com")},
            to=[EmailAddress(email="coord@longridgepartners.com")],
            date=datetime(2026, 4, 14, 9, 0, tzinfo=UTC),
            body_text="Can you schedule a chat?",
        )
        internal_forward = _internal_msg()
        internal_reply = _internal_msg(
            from_email="recruiter@longridgepartners.com",
            to_emails=("coord@longridgepartners.com",),
        )
        thread = [external_inbound, internal_forward, internal_reply]
        # Internal-only check on the *trigger* (most recent) message: true alone…
        assert _is_internal_only(internal_reply) is True
        # …but false once we consider the whole thread.
        assert _is_internal_only(internal_reply, thread) is False


class TestInternalOnlyFilter:
    @pytest.mark.asyncio
    async def test_internal_only_skips_classification_on_unlinked_thread(self):
        """Pure LRP-to-LRP chatter on an unlinked thread shouldn't try to
        spawn a new scheduling loop — that's just noise."""
        router, classifier, agent, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []
        event = EmailEvent(
            message=_internal_msg(cc_emails=("carol@longridgepartners.com",)),
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.OUTGOING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        await router.on_email(event)
        classifier.classify.assert_not_called()
        agent.act.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_participants_routes_normally(self):
        router, classifier, _, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []
        msg = _internal_msg(to_emails=("candidate@gmail.com",))
        event = EmailEvent(
            message=msg,
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        await router.on_email(event)
        classifier.classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_internal_only_on_linked_thread_routes_to_agent(self):
        """Recruiter→coordinator on a live loop is the substance of the work —
        internal LRP-to-LRP traffic on linked threads MUST reach the agent."""
        router, classifier, agent, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = [_loop()]
        event = EmailEvent(
            message=_internal_msg(),
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        await router.on_email(event)
        classifier.classify.assert_not_called()
        agent.act.assert_called_once()

    @pytest.mark.asyncio
    async def test_forwarded_client_request_then_internal_reply_classifies(self):
        """Coordinator forwarded a client's request to a recruiter; the
        recruiter's internal-only reply should still spawn loop classification
        because the thread participants include an external client upstream."""
        router, classifier, _, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []

        external_inbound = Message(
            id="msg0",
            thread_id="thread1",
            subject="Interview request",
            **{"from": EmailAddress(email="client@external.com")},
            to=[EmailAddress(email="alice@longridgepartners.com")],
            date=datetime(2026, 4, 14, 9, 0, tzinfo=UTC),
            body_text="Can you schedule?",
        )
        coord_forward = _internal_msg()  # alice -> bob
        recruiter_reply = _internal_msg(
            from_email="bob@longridgepartners.com",
            to_emails=("alice@longridgepartners.com",),
        )

        event = EmailEvent(
            message=recruiter_reply,
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.REPLY,
            new_participants=[],
            thread_messages=[external_inbound, coord_forward, recruiter_reply],
        )
        await router.on_email(event)
        classifier.classify.assert_called_once()


# --- Off-thread coordinator filter ---


def _off_thread_msg(
    from_email: str,
    to_emails: tuple[str, ...] = (),
    cc_emails: tuple[str, ...] = (),
    msg_id: str = "msg1",
    thread_id: str = "thread1",
) -> Message:
    """Build a Message with arbitrary participants (none of them the coordinator)."""
    return Message(
        id=msg_id,
        thread_id=thread_id,
        subject="Off-thread",
        **{"from": EmailAddress(email=from_email)},
        to=[EmailAddress(email=e) for e in to_emails],
        cc=[EmailAddress(email=e) for e in cc_emails],
        date=datetime(2026, 5, 19, 17, 42, tzinfo=UTC),
        body_text="Body",
    )


class TestCoordinatorOnTrigger:
    """Unit tests for the _coordinator_on_trigger helper."""

    def test_coordinator_on_from(self):
        msg = _off_thread_msg(from_email="coord@lrp.com", to_emails=("ext@example.com",))
        assert _coordinator_on_trigger(msg, "coord@lrp.com") is True

    def test_coordinator_on_to(self):
        msg = _off_thread_msg(
            from_email="ext@example.com", to_emails=("coord@lrp.com", "other@lrp.com")
        )
        assert _coordinator_on_trigger(msg, "coord@lrp.com") is True

    def test_coordinator_on_cc(self):
        msg = _off_thread_msg(
            from_email="ext@example.com",
            to_emails=("other@lrp.com",),
            cc_emails=("coord@lrp.com",),
        )
        assert _coordinator_on_trigger(msg, "coord@lrp.com") is True

    def test_coordinator_absent(self):
        # Mirrors the Fubo / Trump / Eric-pitching-Steve traces: external sender,
        # another LRP staffer on To, coordinator nowhere.
        msg = _off_thread_msg(
            from_email="stream@newsletters.fubo.tv",
            to_emails=("aaron@longridgepartners.com",),
        )
        assert _coordinator_on_trigger(msg, "adam@longridgepartners.com") is False

    def test_case_insensitive(self):
        msg = _off_thread_msg(from_email="ext@example.com", to_emails=("COORD@LRP.COM",))
        assert _coordinator_on_trigger(msg, "coord@lrp.com") is True


class TestOffThreadFilter:
    """Router-level integration: off-thread trigger never reaches the classifier."""

    @pytest.mark.asyncio
    async def test_coordinator_on_to_passes_through(self):
        router, classifier, _, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []

        event = EmailEvent(
            message=_off_thread_msg(from_email="ext@candidate.com", to_emails=("coord@lrp.com",)),
            coordinator_email="coord@lrp.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.NEW_THREAD,
            new_participants=[],
        )
        await router.on_email(event)

        classifier.classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_coordinator_on_cc_passes_through(self):
        router, classifier, _, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []

        event = EmailEvent(
            message=_off_thread_msg(
                from_email="ext@candidate.com",
                to_emails=("other@external.com",),
                cc_emails=("coord@lrp.com",),
            ),
            coordinator_email="coord@lrp.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.NEW_THREAD,
            new_participants=[],
        )
        await router.on_email(event)

        classifier.classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_coordinator_on_from_never_reaches_classifier(self):
        """Outbound from the coordinator — the existing outgoing-on-unlinked
        filter catches this first, so the assertion is just that the classifier
        is never called (robust to which filter trips first)."""
        router, classifier, _, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []

        event = EmailEvent(
            message=_off_thread_msg(from_email="coord@lrp.com", to_emails=("ext@candidate.com",)),
            coordinator_email="coord@lrp.com",
            direction=MessageDirection.OUTGOING,
            message_type=MessageType.NEW_THREAD,
            new_participants=[],
        )
        await router.on_email(event)

        classifier.classify.assert_not_called()

    @pytest.mark.asyncio
    async def test_off_thread_marketing_blast_is_blocked(self, caplog):
        """Mirrors the Fubo / Trump / pitched-candidate traces: marketing /
        forwarded mail in the coordinator's mailbox where they aren't on
        From/To/Cc must never reach the loop classifier."""
        import logging

        router, classifier, _, loop_service = _make_router()
        loop_service.find_loops_by_thread.return_value = []

        event = EmailEvent(
            message=_off_thread_msg(
                from_email="stream@newsletters.fubo.tv",
                to_emails=("aaron@longridgepartners.com",),
                msg_id="msg_fubo",
                thread_id="thread_fubo",
            ),
            coordinator_email="adam@longridgepartners.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.NEW_THREAD,
            new_participants=[],
        )
        with caplog.at_level(logging.INFO, logger="api.classifier.router"):
            await router.on_email(event)

        classifier.classify.assert_not_called()
        assert any("skipping off-thread message" in r.message for r in caplog.records)


# --- Deduplication ---


def _pending_suggestion(
    loop_id="lop_1",
    action=SuggestedAction.ADVANCE_STAGE,
    action_data=None,
) -> Suggestion:
    if action_data is None:
        action_data = {"target_stage": "awaiting_client"}
    return Suggestion(
        id="sug_existing",
        coordinator_email="coord@lrp.com",
        gmail_message_id="msg0",
        gmail_thread_id="thread1",
        loop_id=loop_id,
        classification=EmailClassification.AVAILABILITY_RESPONSE,
        action=action,
        confidence=0.9,
        summary="Existing suggestion",
        action_data=action_data,
        status=SuggestionStatus.PENDING,
    )


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_of_existing_pending_is_skipped(self):
        agent, suggestion_service = _make_agent()
        existing = _pending_suggestion(
            action=SuggestedAction.ADVANCE_STAGE,
            action_data={"target_stage": "awaiting_client"},
        )
        suggestion_service.get_pending_for_loop.return_value = [existing]

        items = [
            _suggestion_item(
                action=SuggestedAction.ADVANCE_STAGE,
                target_stage=StageState.AWAITING_CLIENT,
            )
        ]

        with _patch_agent_llm(agent, _suggestions_envelope(items)):
            await agent.act(_event(), [_loop()])

        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_batch_duplicate_only_creates_first(self):
        agent, suggestion_service = _make_agent()

        item = _suggestion_item(
            action=SuggestedAction.DRAFT_EMAIL,
            action_data={"body": "Hi there", "recipient_type": "recruiter"},
        )

        with _patch_agent_llm(agent, _suggestions_envelope([item, item])):
            await agent.act(_event(), [_loop()])

        assert suggestion_service.create_suggestion.call_count == 1

    @pytest.mark.asyncio
    async def test_different_action_data_not_deduplicated(self):
        agent, suggestion_service = _make_agent()
        existing = _pending_suggestion(
            action=SuggestedAction.ADVANCE_STAGE,
            action_data={"target_stage": "awaiting_client"},
        )
        suggestion_service.get_pending_for_loop.return_value = [existing]

        items = [
            _suggestion_item(
                action=SuggestedAction.ADVANCE_STAGE,
                target_stage=StageState.SCHEDULED,
                action_data={"target_stage": "scheduled"},
            )
        ]

        with _patch_agent_llm(agent, _suggestions_envelope(items)):
            await agent.act(_event(), [_loop()])

        suggestion_service.create_suggestion.assert_called_once()


# --- XML formatters ---


class TestXMLFormatters:
    def test_email_xml_escapes_special_chars(self):
        from api.classifier.formatters import format_email_xml

        msg = Message(
            id="msg1",
            thread_id="thread1",
            subject="Re: Q1 & Q2 <urgent>",
            **{"from": EmailAddress(name="A & B", email="a@example.com")},
            to=[EmailAddress(email="coord@lrp.com")],
            date=datetime(2026, 5, 5, 11, 30, tzinfo=UTC),
            body_text="Body with <tag> & ampersand",
        )
        xml = format_email_xml(msg, "incoming")
        assert "<email direction='inbound'>" in xml
        assert "&amp;" in xml
        assert "&lt;tag&gt;" in xml
        assert "<from>A &amp; B a@example.com</from>" in xml
        # No raw '<' or '>' in interpolated values
        assert "<tag>" not in xml.replace("<email", "").replace("</email>", "")

    def test_email_xml_omits_empty_cc(self):
        from api.classifier.formatters import format_email_xml

        msg = Message(
            id="msg1",
            thread_id="thread1",
            subject="Hi",
            **{"from": EmailAddress(email="a@example.com")},
            to=[EmailAddress(email="b@example.com")],
            cc=[],
            date=datetime(2026, 5, 5, 11, 30, tzinfo=UTC),
            body_text="Hello",
        )
        xml = format_email_xml(msg, "outgoing")
        assert "<cc>" not in xml
        assert "<email direction='outbound'>" in xml

    def test_thread_history_oldest_first(self):
        from api.classifier.formatters import format_thread_history_xml

        msgs = [
            Message(
                id=f"msg{i}",
                thread_id="thread1",
                subject="Re: Hi",
                **{"from": EmailAddress(email="a@example.com")},
                to=[EmailAddress(email="coord@lrp.com")],
                date=datetime(2026, 5, i, 9, 0, tzinfo=UTC),
                body_text=f"Body {i}",
            )
            for i in (3, 5, 7)
        ]
        xml = format_thread_history_xml(
            msgs, current_message_id="msg7", coordinator_email="coord@lrp.com"
        )
        # Oldest body should appear before the newer one.
        assert xml.index("Body 3") < xml.index("Body 5")
        # Excludes the current message.
        assert "Body 7" not in xml

    def test_pending_update_actor_renders_role(self):
        from api.classifier.formatters import format_loop_xml

        loop = _loop()
        pending = [
            Suggestion(
                id="sug_upd",
                coordinator_email="coord@lrp.com",
                gmail_message_id="msg0",
                gmail_thread_id="thread1",
                loop_id=loop.id,
                classification=EmailClassification.FOLLOW_UP_NEEDED,
                action=SuggestedAction.UPDATE_ACTOR,
                confidence=0.7,
                summary="The recruiter on file looks wrong",
                action_data={"role": "recruiter"},
                status=SuggestionStatus.PENDING,
            ),
        ]
        xml = format_loop_xml(loop, pending)
        assert "sug_upd" in xml
        assert "<role>recruiter</role>" in xml

    def test_loop_xml_renders_actors_and_pending(self):
        from api.classifier.formatters import format_loop_xml

        loop = _loop()
        pending = [
            Suggestion(
                id="sug_draft",
                coordinator_email="coord@lrp.com",
                gmail_message_id="msg0",
                gmail_thread_id="thread1",
                loop_id=loop.id,
                classification=EmailClassification.AVAILABILITY_RESPONSE,
                action=SuggestedAction.DRAFT_EMAIL,
                confidence=0.8,
                summary="Stale draft summary",
                action_data={
                    "body": "Hey Rachel, can you send Jordan's avails?",
                    "recipient_type": "recruiter",
                },
                status=SuggestionStatus.PENDING,
            ),
            Suggestion(
                id="sug_ask",
                coordinator_email="coord@lrp.com",
                gmail_message_id="msg0",
                gmail_thread_id="thread1",
                loop_id=loop.id,
                classification=EmailClassification.FOLLOW_UP_NEEDED,
                action=SuggestedAction.ASK_COORDINATOR,
                confidence=0.7,
                summary="Need a tiebreaker",
                action_data={"question": "Should we wait for Jordan or use Drew?"},
                status=SuggestionStatus.PENDING,
            ),
        ]
        xml = format_loop_xml(loop, pending)
        assert f"<loop id='{loop.id}'>" in xml
        assert "<stage>awaiting_candidate</stage>" in xml
        assert "<candidate>John Smith</candidate>" in xml
        assert "<recruiter>Bob (bob@lrp.com)</recruiter>" in xml
        assert "<client-contact>Jane (jane@hf.com), HF Co</client-contact>" in xml
        # DRAFT_EMAIL renders body + recipient_type for dedup-by-content + targeting.
        assert "sug_draft" in xml
        assert "Stale draft summary" in xml
        assert "<recipient-type>recruiter</recipient-type>" in xml
        assert "Hey Rachel" in xml
        # ASK_COORDINATOR renders the question.
        assert "sug_ask" in xml
        assert "<question>Should we wait for Jordan or use Drew?</question>" in xml


# --- Batch guardrails ---


def _two_loop_thread() -> list[Loop]:
    """Two loops on the same thread with two distinct recruiters."""
    return [
        Loop(
            id=f"lop_{i}",
            coordinator_id="crd_1",
            client_contact_id="cli_1",
            recruiter_id=f"con_{i}",
            candidate_id=f"can_{i}",
            title=f"Loop {i}",
            state=StageState.AWAITING_CANDIDATE,
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
            updated_at=datetime(2026, 4, 14, tzinfo=UTC),
            candidate=Candidate(
                id=f"can_{i}",
                name=f"Candidate {i}",
                created_at=datetime(2026, 4, 10, tzinfo=UTC),
            ),
            recruiter=Contact(
                id=f"con_{i}",
                name=f"Recruiter {i}",
                email=f"rec{i}@lrp.com",
                role="recruiter",
                created_at=datetime(2026, 4, 10, tzinfo=UTC),
            ),
        )
        for i in (1, 2)
    ]


class TestBatchGuardrails:
    def test_multiple_client_drafts_rejected(self):
        agent, _ = _make_agent()
        loops = _two_loop_thread()
        items = [
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                target_loop_id="lop_1",
                action_data={"body": "A", "recipient_type": "client"},
            ),
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                target_loop_id="lop_2",
                action_data={"body": "B", "recipient_type": "client"},
            ),
        ]
        _, errors = agent._validate_batch(items, loops, [])
        assert any("client draft emails" in e for e in errors)


# --- Conversation retry ---


class TestConversationRetry:
    @pytest.mark.asyncio
    async def test_coordinator_response_splices_prior_turn(self):
        agent, _suggestion_service = _make_agent()

        items = [
            _suggestion_item(
                action=SuggestedAction.ADVANCE_STAGE,
                target_stage=StageState.AWAITING_CLIENT,
            )
        ]
        agent._llm.complete = AsyncMock(return_value=_llm_response(_suggestions_envelope(items)))

        originating = Suggestion(
            id="sug_q",
            coordinator_email="coord@lrp.com",
            gmail_message_id="msg0",
            gmail_thread_id="thread1",
            loop_id="lop_1",
            classification=EmailClassification.FOLLOW_UP_NEEDED,
            action=SuggestedAction.ASK_COORDINATOR,
            confidence=0.7,
            summary="Need decision",
            action_data={"question": "Pick a time slot?"},
            status=SuggestionStatus.ACCEPTED,
        )

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(
                _event(),
                [_loop()],
                coordinator_response="Pick 3pm",
                originating_suggestion=originating,
            )

        messages = agent._llm.complete.await_args.kwargs["messages"]
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "user"]
        assert "Pick 3pm" in messages[-1]["content"]
        assert "ask_coordinator" in messages[-2]["content"]


# --- Expire suggestion via act() ---


class TestExpireSuggestionFlow:
    @pytest.mark.asyncio
    async def test_expire_suggestion_drops_unknown_target(self):
        agent, suggestion_service = _make_agent()

        items = [
            _suggestion_item(
                action=SuggestedAction.EXPIRE_SUGGESTION,
                action_data={"suggestion_id": "sug_never_existed"},
            )
        ]
        # No pending suggestions on the loop — and we don't allow retry to make the failure stick.
        agent._llm.complete = AsyncMock(
            side_effect=[
                _llm_response(_suggestions_envelope(items)),
                # Retry produces the same invalid response.
                _llm_response(_suggestions_envelope(items)),
            ]
        )

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(_event(), [_loop()])

        suggestion_service.create_suggestion.assert_not_called()


# --- Rejection re-run ---


def _rejected_draft_suggestion() -> Suggestion:
    return Suggestion(
        id="sug_rejected",
        coordinator_email="coord@lrp.com",
        gmail_message_id="msg0",
        gmail_thread_id="thread1",
        loop_id="lop_1",
        classification=EmailClassification.AVAILABILITY_RESPONSE,
        action=SuggestedAction.DRAFT_EMAIL,
        confidence=0.8,
        summary="Drafted to recruiter",
        action_data={"body": "Original body", "recipient_type": "recruiter"},
        status=SuggestionStatus.REJECTED,
    )


class TestRejectionRerun:
    @pytest.mark.asyncio
    async def test_rejection_splices_prior_turn_and_followup(self):
        agent, _ = _make_agent()

        replacement = [
            _suggestion_item(
                action=SuggestedAction.ASK_COORDINATOR,
                action_data={"question": "How should I handle this differently?"},
            )
        ]
        agent._llm.complete = AsyncMock(
            return_value=_llm_response(_suggestions_envelope(replacement))
        )

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(
                _event(),
                [_loop()],
                rejected_suggestion=_rejected_draft_suggestion(),
            )

        messages = agent._llm.complete.await_args.kwargs["messages"]
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "user"]
        # The rejected suggestion appears in the reconstructed assistant turn.
        assert "draft_email" in messages[-2]["content"]
        # The follow-up explicitly tells the agent it was rejected with no reason.
        followup = messages[-1]["content"]
        assert "rejected" in followup.lower()
        assert "materially different" in followup.lower()
        assert "no_action" in followup

    @pytest.mark.asyncio
    async def test_rejection_dedups_identical_resuggestion(self):
        """Agent ignores the prompt and re-emits the exact same DRAFT_EMAIL —
        the dedup fingerprint should drop it silently."""
        agent, suggestion_service = _make_agent()

        rejected = _rejected_draft_suggestion()
        # Agent re-emits the rejected suggestion verbatim.
        duplicate = _suggestion_item(
            action=SuggestedAction.DRAFT_EMAIL,
            action_data=rejected.action_data,
        )
        agent._llm.complete = AsyncMock(
            return_value=_llm_response(_suggestions_envelope([duplicate]))
        )

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(_event(), [_loop()], rejected_suggestion=rejected)

        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejection_accepts_different_alternative(self):
        """Agent proposes a materially different action — that suggestion is persisted."""
        agent, suggestion_service = _make_agent()

        rejected = _rejected_draft_suggestion()
        alternative = [
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                action_data={"body": "Totally different body", "recipient_type": "client"},
            )
        ]
        agent._llm.complete = AsyncMock(
            return_value=_llm_response(_suggestions_envelope(alternative))
        )

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(_event(), [_loop()], rejected_suggestion=rejected)

        suggestion_service.create_suggestion.assert_called_once()


class TestGenerationCancellation:
    """Per-thread generation tokens supersede in-flight agent runs.

    Each worker handler claims a fresh token before calling agent.act().
    The agent checks before the LLM round-trip and again before persisting
    suggestions. If a newer worker has overwritten the token in Redis,
    the older run logs and returns without writing.
    """

    def _redis_mock(self, current_token_per_get: list[str | None]):
        """Build a redis mock whose .get() returns the next value in the list
        on each call. Lets a test simulate the token flipping between
        checkpoints."""
        # iter exhausts after len(list); subsequent calls would StopIteration —
        # tests should provide enough values for the calls they expect.
        responses = iter(current_token_per_get)

        async def _get(_key):
            return next(responses)

        redis = MagicMock()
        redis.get = AsyncMock(side_effect=_get)
        return redis

    @pytest.mark.asyncio
    async def test_aborts_before_llm_when_superseded(self):
        """If the token in Redis doesn't match ours before the LLM call,
        skip the round-trip entirely. No LLM call, no persist."""
        agent, suggestion_service = _make_agent()
        agent._llm.complete = AsyncMock()  # should never be called
        redis = self._redis_mock(["different-token-already"])

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(
                _event(),
                [_loop()],
                arq_pool=redis,
                generation_token="our-token",
                generation_thread_id="thread1",
            )

        agent._llm.complete.assert_not_awaited()
        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_aborts_before_persist_when_superseded_mid_llm(self):
        """LLM call succeeds, but a newer worker overwrote the token while we
        were waiting. Drop the (now-stale) suggestions; do not persist."""
        agent, suggestion_service = _make_agent()
        item = _suggestion_item(action=SuggestedAction.ADVANCE_STAGE)
        agent._llm.complete = AsyncMock(return_value=_llm_response(_suggestions_envelope([item])))
        # Checkpoint 1 still sees our token → continue.
        # Checkpoint 2 sees a different token → abort.
        redis = self._redis_mock(["our-token", "superseded-by-newer-worker"])

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(
                _event(),
                [_loop()],
                arq_pool=redis,
                generation_token="our-token",
                generation_thread_id="thread1",
            )

        agent._llm.complete.assert_awaited_once()
        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_to_completion_when_token_stays_current(self):
        """Token matches at both checkpoints → suggestions persisted."""
        agent, suggestion_service = _make_agent()
        item = _suggestion_item(action=SuggestedAction.ADVANCE_STAGE)
        agent._llm.complete = AsyncMock(return_value=_llm_response(_suggestions_envelope([item])))
        redis = self._redis_mock(["our-token", "our-token"])

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(
                _event(),
                [_loop()],
                arq_pool=redis,
                generation_token="our-token",
                generation_thread_id="thread1",
            )

        suggestion_service.create_suggestion.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_redis_falls_open(self):
        """When arq_pool is None (Redis unavailable or test path that doesn't
        wire it), the checkpoints fail open and the run proceeds normally."""
        agent, suggestion_service = _make_agent()
        item = _suggestion_item(action=SuggestedAction.ADVANCE_STAGE)
        agent._llm.complete = AsyncMock(return_value=_llm_response(_suggestions_envelope([item])))

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(
                _event(),
                [_loop()],
                arq_pool=None,
                generation_token=None,
                generation_thread_id=None,
            )

        suggestion_service.create_suggestion.assert_called_once()

    @pytest.mark.asyncio
    async def test_token_missing_in_redis_treated_as_superseded(self):
        """TTL expired (or someone cleared the key) → stored is None →
        is_current_generation returns False → abort before LLM."""
        agent, suggestion_service = _make_agent()
        agent._llm.complete = AsyncMock()
        redis = self._redis_mock([None])

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(
                _event(),
                [_loop()],
                arq_pool=redis,
                generation_token="our-token",
                generation_thread_id="thread1",
            )

        agent._llm.complete.assert_not_awaited()
        suggestion_service.create_suggestion.assert_not_called()
