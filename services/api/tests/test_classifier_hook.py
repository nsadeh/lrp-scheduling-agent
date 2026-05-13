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
from api.classifier.router import EmailRouter, _is_internal_only
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


def _llm_response(content: str):
    """Stub for the LLMResponse return value from LLMService.complete()."""
    resp = MagicMock()
    resp.content = content
    resp.model = "test-model"
    resp.provider = "test"
    resp.latency_ms = 1.0
    return resp


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


# --- Error handling ---


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_classifier_llm_failure_creates_needs_attention(self):
        classifier, suggestion_service = _make_classifier()

        with patch(
            "api.classifier.loop_classifier.classify_new_thread",
            new_callable=AsyncMock,
            side_effect=Exception("LLM down"),
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


class TestInternalOnlyFilter:
    @pytest.mark.asyncio
    async def test_all_internal_skips_classification(self):
        router, classifier, agent, loop_service = _make_router()
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
        loop_service.find_loops_by_thread.assert_not_called()

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
    async def test_internal_only_skips_even_on_linked_thread(self):
        router, classifier, agent, loop_service = _make_router()
        event = EmailEvent(
            message=_internal_msg(),
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        await router.on_email(event)
        classifier.classify.assert_not_called()
        agent.act.assert_not_called()
        loop_service.find_loops_by_thread.assert_not_called()


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

    def test_loop_xml_renders_actors_and_pending(self):
        from api.classifier.formatters import format_loop_xml

        loop = _loop()
        pending = [
            Suggestion(
                id="sug_old",
                coordinator_email="coord@lrp.com",
                gmail_message_id="msg0",
                gmail_thread_id="thread1",
                loop_id=loop.id,
                classification=EmailClassification.AVAILABILITY_RESPONSE,
                action=SuggestedAction.DRAFT_EMAIL,
                confidence=0.8,
                summary="Stale draft summary",
                action_data={"body": "...", "recipient_type": "recruiter"},
                status=SuggestionStatus.PENDING,
            )
        ]
        xml = format_loop_xml(loop, pending)
        assert f"<loop id='{loop.id}'>" in xml
        assert "<stage>awaiting_candidate</stage>" in xml
        assert "<candidate>John Smith</candidate>" in xml
        assert "<recruiter>Bob</recruiter>" in xml
        assert "<client-contact>Jane, HF Co</client-contact>" in xml
        assert "sug_old" in xml
        assert "Stale draft summary" in xml


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
    def test_recruiter_draft_count_exceeds_known_recruiters(self):
        agent, _ = _make_agent()
        loops = [_loop()]  # one recruiter on the thread
        items = [
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                action_data={"body": "A", "recipient_type": "recruiter"},
            ),
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                action_data={"body": "B", "recipient_type": "recruiter"},
            ),
        ]
        _, errors = agent._validate_batch(items, loops, [])
        assert any("recruiter draft emails" in e for e in errors)

    def test_recruiter_drafts_match_recruiter_count(self):
        agent, _ = _make_agent()
        loops = _two_loop_thread()
        items = [
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                target_loop_id="lop_1",
                action_data={"body": "A", "recipient_type": "recruiter"},
            ),
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                target_loop_id="lop_2",
                action_data={"body": "B", "recipient_type": "recruiter"},
            ),
        ]
        _, errors = agent._validate_batch(items, loops, [])
        assert not [e for e in errors if "recruiter draft" in e]

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
    async def test_batch_error_triggers_followup_with_history(self):
        agent, suggestion_service = _make_agent()
        loops = [_loop()]

        # First call: two recruiter drafts (violates batch guardrail).
        bad_items = [
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                action_data={"body": "A", "recipient_type": "recruiter"},
            ),
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                action_data={"body": "B", "recipient_type": "recruiter"},
            ),
        ]
        # Retry call: one recruiter draft (valid).
        good_items = [
            _suggestion_item(
                action=SuggestedAction.DRAFT_EMAIL,
                action_data={"body": "Combined", "recipient_type": "recruiter"},
            ),
        ]

        responses = [
            _llm_response(_suggestions_envelope(bad_items)),
            _llm_response(_suggestions_envelope(good_items)),
        ]
        agent._llm.complete = AsyncMock(side_effect=responses)

        with patch(
            "api.classifier.next_action_agent.fetch_prompt",
            return_value=_FakeTextPrompt(),
        ):
            await agent.act(_event(), loops)

        # Two LLM calls: original + retry.
        assert agent._llm.complete.await_count == 2

        # Second call's messages must include the original + assistant turn + error follow-up.
        retry_messages = agent._llm.complete.await_args_list[1].kwargs["messages"]
        roles = [m["role"] for m in retry_messages]
        assert roles == ["system", "user", "assistant", "user"]
        assert "errors" in retry_messages[-1]["content"].lower()

        suggestion_service.create_suggestion.assert_called_once()

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
