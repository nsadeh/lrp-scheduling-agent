"""Tests for the two-stage classification pipeline: Router, LoopClassifier, NextActionAgent."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.classifier.loop_classifier import LoopClassifier, _resolve_coordinator_name
from api.classifier.models import (
    ClassificationResult,
    EmailClassification,
    SuggestedAction,
    SuggestionItem,
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
            action_data = {"directive": "Draft something", "recipient_type": "recruiter"}
        elif action == SuggestedAction.ASK_COORDINATOR:
            action_data = {"question": "What should I do?"}
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
        result, error = agent._apply_guardrails(item)
        assert result.action == SuggestedAction.NO_ACTION
        assert error is not None
        assert "not allowed" in error

    def test_link_thread_blacklisted(self):
        agent, _ = _make_agent()
        item = _suggestion_item(action=SuggestedAction.LINK_THREAD)
        result, error = agent._apply_guardrails(item)
        assert result.action == SuggestedAction.NO_ACTION
        assert error is not None

    def test_advance_stage_passes(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.ADVANCE_STAGE,
            target_stage=StageState.AWAITING_CLIENT,
        )
        result, error = agent._apply_guardrails(item)
        assert result.action == SuggestedAction.ADVANCE_STAGE
        assert error is None

    def test_draft_email_passes(self):
        agent, _ = _make_agent()
        item = _suggestion_item(action=SuggestedAction.DRAFT_EMAIL)
        result, error = agent._apply_guardrails(item)
        assert result.action == SuggestedAction.DRAFT_EMAIL
        assert error is None

    def test_missing_target_loop_id_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(action=SuggestedAction.DRAFT_EMAIL, target_loop_id=None)
        result, error = agent._apply_guardrails(item)
        assert result.action == SuggestedAction.NO_ACTION
        assert error is not None
        assert "target_loop_id" in error

    def test_invalid_action_data_fails(self):
        agent, _ = _make_agent()
        item = _suggestion_item(
            action=SuggestedAction.ADVANCE_STAGE,
            action_data={},  # missing target_stage
        )
        result, error = agent._apply_guardrails(item)
        assert result.action == SuggestedAction.NO_ACTION
        assert error is not None
        assert "action_data" in error


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

        with patch(
            "api.classifier.next_action_agent.determine_next_action",
            new_callable=AsyncMock,
            side_effect=Exception("LLM down"),
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
