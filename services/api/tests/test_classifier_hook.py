"""Tests for ClassifierHook — guardrails, outgoing skip, error handling."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.classifier.hook import ClassifierHook, _is_internal_only, _resolve_coordinator_name
from api.classifier.models import (
    ClassificationResult,
    EmailClassification,
    SuggestedAction,
    SuggestionItem,
)
from api.classifier.sender_blacklist import SenderBlacklist
from api.gmail.hooks import EmailEvent, MessageDirection, MessageType
from api.gmail.models import EmailAddress, Message
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    Stage,
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


def _loop(loop_id="lop_1", stage_state=StageState.AWAITING_CANDIDATE) -> Loop:
    return Loop(
        id=loop_id,
        coordinator_id="crd_1",
        client_contact_id="cli_1",
        recruiter_id="con_1",
        candidate_id="can_1",
        title="Round 1 - John Smith",
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
        stages=[
            Stage(
                id="stg_1",
                loop_id=loop_id,
                name="Round 1",
                state=stage_state,
                ordinal=0,
                created_at=datetime(2026, 4, 10, tzinfo=UTC),
                updated_at=datetime(2026, 4, 14, tzinfo=UTC),
            ),
        ],
    )


def _suggestion_item(
    classification=EmailClassification.AVAILABILITY_RESPONSE,
    action=SuggestedAction.ADVANCE_STAGE,
    confidence=0.95,
    target_state=StageState.AWAITING_CLIENT,
    auto_advance=False,
) -> SuggestionItem:
    return SuggestionItem(
        classification=classification,
        action=action,
        confidence=confidence,
        summary="Test suggestion",
        target_state=target_state,
        auto_advance=auto_advance,
    )


def _classification_result(items=None, reasoning="test"):
    return ClassificationResult(
        suggestions=items or [_suggestion_item()],
        reasoning=reasoning,
    )


def _make_hook(sender_blacklist: SenderBlacklist | None = None):
    """Create a ClassifierHook with mocked dependencies."""
    llm = MagicMock()
    langfuse = MagicMock()
    suggestion_service = MagicMock()
    suggestion_service.create_suggestion = AsyncMock(return_value=MagicMock(id="sug_test"))
    suggestion_service.supersede_pending_for_loop = AsyncMock()

    loop_service = MagicMock()
    loop_service.find_loop_by_thread = AsyncMock(return_value=None)
    loop_service.find_loops_by_thread = AsyncMock(return_value=[])
    loop_service.get_coordinator_by_email = AsyncMock(return_value=None)
    loop_service.get_events = AsyncMock(return_value=[])
    loop_service.advance_stage = AsyncMock()

    hook = ClassifierHook(
        llm=llm,
        langfuse=langfuse,
        suggestion_service=suggestion_service,
        loop_service=loop_service,
        sender_blacklist=sender_blacklist,
    )
    return hook, llm, langfuse, suggestion_service, loop_service


class TestGuardrails:
    def test_link_thread_below_threshold_converts_to_create_loop(self):
        hook, *_ = _make_hook()
        item = _suggestion_item(
            action=SuggestedAction.LINK_THREAD,
            confidence=0.8,
        )
        result = hook._apply_guardrails(item, None)
        assert result.action == SuggestedAction.CREATE_LOOP
        assert "confidence too low" in result.summary

    def test_link_thread_above_threshold_passes(self):
        hook, *_ = _make_hook()
        item = _suggestion_item(
            action=SuggestedAction.LINK_THREAD,
            confidence=0.95,
        )
        result = hook._apply_guardrails(item, None)
        assert result.action == SuggestedAction.LINK_THREAD

    def test_invalid_transition_demotes_to_ask_coordinator(self):
        hook, *_ = _make_hook()
        loop = _loop(stage_state=StageState.AWAITING_CANDIDATE)
        # AWAITING_CANDIDATE cannot go to SCHEDULED directly
        item = _suggestion_item(
            action=SuggestedAction.ADVANCE_STAGE,
            target_state=StageState.SCHEDULED,
        )
        result = hook._apply_guardrails(item, loop)
        assert result.action == SuggestedAction.ASK_COORDINATOR
        assert "not allowed" in result.questions[0]

    def test_valid_transition_passes(self):
        hook, *_ = _make_hook()
        loop = _loop(stage_state=StageState.AWAITING_CANDIDATE)
        # AWAITING_CANDIDATE → AWAITING_CLIENT is valid
        item = _suggestion_item(
            action=SuggestedAction.ADVANCE_STAGE,
            target_state=StageState.AWAITING_CLIENT,
        )
        result = hook._apply_guardrails(item, loop)
        assert result.action == SuggestedAction.ADVANCE_STAGE


class TestOutgoingSkip:
    @pytest.mark.asyncio
    async def test_outgoing_on_unlinked_thread_skips(self):
        hook, _, _, suggestion_service, loop_service = _make_hook()
        loop_service.find_loop_by_thread.return_value = None
        loop_service.find_loops_by_thread.return_value = []

        event = _event(direction=MessageDirection.OUTGOING)
        await hook.on_email(event)

        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_outgoing_on_linked_thread_classifies(self):
        hook, _, _, suggestion_service, loop_service = _make_hook()
        loop_service.find_loop_by_thread.return_value = _loop()
        loop_service.find_loops_by_thread.return_value = [_loop()]

        with patch(
            "api.classifier.hook.classify_email",
            new_callable=AsyncMock,
            return_value=_classification_result(
                [
                    _suggestion_item(auto_advance=True),
                ]
            ),
        ):
            event = _event(direction=MessageDirection.OUTGOING)
            await hook.on_email(event)

        suggestion_service.create_suggestion.assert_called_once()


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_llm_failure_creates_needs_attention(self):
        hook, _, _, suggestion_service, loop_service = _make_hook()
        loop_service.find_loop_by_thread.return_value = None
        loop_service.find_loops_by_thread.return_value = []

        with patch(
            "api.classifier.hook.classify_email",
            new_callable=AsyncMock,
            side_effect=Exception("LLM down"),
        ):
            event = _event()
            await hook.on_email(event)

        suggestion_service.create_suggestion.assert_called_once()
        call_kwargs = suggestion_service.create_suggestion.call_args.kwargs
        assert call_kwargs["item"].action == SuggestedAction.ASK_COORDINATOR
        assert call_kwargs["item"].confidence == 0.0


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
        # Incoming email: coordinator is in `to`. DB row absent.
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
        # Outgoing email: coordinator is `from_`. DB row absent.
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
        # No DB row, no display name on headers.
        event = _event()  # to: [EmailAddress(email="coord@lrp.com")] — no name
        assert _resolve_coordinator_name(event, None) == "coord"


class TestSenderBlacklist:
    """Pre-classifier sender blacklist — silent skip on unlinked threads."""

    @pytest.mark.asyncio
    async def test_blacklisted_sender_on_unlinked_thread_skips(self):
        blacklist = SenderBlacklist(domains=frozenset({"withintelligence-email.com"}))
        hook, _, _, suggestion_service, loop_service = _make_hook(sender_blacklist=blacklist)
        loop_service.find_loop_by_thread.return_value = None
        loop_service.find_loops_by_thread.return_value = []

        # classify_email patched only to assert it isn't called
        with patch(
            "api.classifier.hook.classify_email",
            new_callable=AsyncMock,
        ) as mock_classify:
            event = _event(from_email="alerts@withintelligence-email.com")
            await hook.on_email(event)

        mock_classify.assert_not_called()
        suggestion_service.create_suggestion.assert_not_called()

    @pytest.mark.asyncio
    async def test_blacklisted_sender_on_linked_thread_still_classifies(self):
        """Linked threads bypass the blacklist — newsletters forwarded into an
        active candidate conversation should still be classified."""
        blacklist = SenderBlacklist(domains=frozenset({"withintelligence-email.com"}))
        hook, _, _, suggestion_service, loop_service = _make_hook(sender_blacklist=blacklist)
        loop_service.find_loop_by_thread.return_value = _loop()
        loop_service.find_loops_by_thread.return_value = [_loop()]

        with patch(
            "api.classifier.hook.classify_email",
            new_callable=AsyncMock,
            return_value=_classification_result(),
        ) as mock_classify:
            event = _event(from_email="alerts@withintelligence-email.com")
            await hook.on_email(event)

        mock_classify.assert_called_once()
        suggestion_service.create_suggestion.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_blacklisted_sender_classifies_normally(self):
        # The sender domain is NOT in the blacklist — the LLM should run.
        # (Whether a suggestion ultimately persists is a downstream guardrail
        # decision unrelated to the blacklist; we only assert classify was called.)
        blacklist = SenderBlacklist(domains=frozenset({"withintelligence-email.com"}))
        hook, _, _, _, loop_service = _make_hook(sender_blacklist=blacklist)
        loop_service.find_loop_by_thread.return_value = None
        loop_service.find_loops_by_thread.return_value = []

        with patch(
            "api.classifier.hook.classify_email",
            new_callable=AsyncMock,
            return_value=_classification_result(),
        ) as mock_classify:
            event = _event(from_email="alice@candidate.com")
            await hook.on_email(event)

        mock_classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_blacklist_passed_uses_empty_default(self):
        """When no blacklist is injected, default is empty — nothing is blocked."""
        hook, _, _, _, loop_service = _make_hook(sender_blacklist=None)
        loop_service.find_loop_by_thread.return_value = None
        loop_service.find_loops_by_thread.return_value = []

        with patch(
            "api.classifier.hook.classify_email",
            new_callable=AsyncMock,
            return_value=_classification_result(),
        ) as mock_classify:
            event = _event(from_email="alerts@withintelligence-email.com")
            await hook.on_email(event)

        mock_classify.assert_called_once()


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
        hook, _, _, suggestion_service, loop_service = _make_hook()
        event = EmailEvent(
            message=_internal_msg(cc_emails=("carol@longridgepartners.com",)),
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.OUTGOING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        with patch("api.classifier.hook.classify_email", new_callable=AsyncMock) as mock_classify:
            await hook.on_email(event)
        mock_classify.assert_not_called()
        suggestion_service.create_suggestion.assert_not_called()
        loop_service.find_loops_by_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_participants_classifies(self):
        hook, _, _, _, loop_service = _make_hook()
        loop_service.find_loops_by_thread.return_value = []
        msg = _internal_msg(to_emails=("candidate@gmail.com",))
        event = EmailEvent(
            message=msg,
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        with patch(
            "api.classifier.hook.classify_email",
            new_callable=AsyncMock,
            return_value=_classification_result(),
        ) as mock_classify:
            await hook.on_email(event)
        mock_classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_internal_only_skips_even_on_linked_thread(self):
        hook, _, _, suggestion_service, loop_service = _make_hook()
        event = EmailEvent(
            message=_internal_msg(),
            coordinator_email="alice@longridgepartners.com",
            direction=MessageDirection.INCOMING,
            message_type=MessageType.REPLY,
            new_participants=[],
        )
        with patch("api.classifier.hook.classify_email", new_callable=AsyncMock) as mock_classify:
            await hook.on_email(event)
        mock_classify.assert_not_called()
        suggestion_service.create_suggestion.assert_not_called()
        loop_service.find_loops_by_thread.assert_not_called()
