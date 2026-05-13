"""Tests for DraftService — recipient routing, draft creation, lifecycle."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.classifier.models import EmailClassification, Suggestion, SuggestionStatus
from api.drafts.models import DraftStatus
from api.drafts.service import DraftService, _is_forward_draft, _row_to_draft, resolve_recipients
from api.gmail.models import EmailAddress, Message
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    StageState,
)


def _loop(
    loop_id="lop_1",
    state=StageState.AWAITING_CANDIDATE,
    with_client_manager=False,
) -> Loop:
    return Loop(
        id=loop_id,
        coordinator_id="crd_1",
        client_contact_id="cli_1",
        recruiter_id="con_1",
        candidate_id="can_1",
        title="Round 1 - John Smith",
        state=state,
        notes=None,
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
        updated_at=datetime(2026, 4, 15, tzinfo=UTC),
        coordinator=Coordinator(
            id="crd_1",
            name="Fiona",
            email="fiona@lrp.com",
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        client_contact=ClientContact(
            id="cli_1",
            name="Haley",
            email="haley@client.com",
            company="Hedge Fund Co",
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        recruiter=Contact(
            id="con_1",
            name="Mike",
            email="mike@recruiter.com",
            role="recruiter",
            company=None,
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        client_manager=Contact(
            id="con_2",
            name="Sarah",
            email="sarah@lrp.com",
            role="client_manager",
            company=None,
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        )
        if with_client_manager
        else None,
        candidate=Candidate(
            id="can_1",
            name="John Smith",
            notes=None,
            created_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        email_threads=[],
    )


def _suggestion(
    sug_id="sug_1",
    classification=EmailClassification.AVAILABILITY_RESPONSE,
) -> Suggestion:
    return Suggestion(
        id=sug_id,
        coordinator_email="fiona@lrp.com",
        gmail_message_id="msg_1",
        gmail_thread_id="thread_1",
        loop_id="lop_1",
        classification=classification,
        action="draft_email",
        confidence=0.95,
        summary="Share availability with client",
        action_data={
            "body": "Hi Haley, John is available (in ET): Mon 3/2: 8am-11am.",
            "recipient_type": "client",
        },
        reasoning="Availability response detected",
        status=SuggestionStatus.PENDING,
        resolved_at=None,
        resolved_by=None,
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


def _draft_row(
    draft_id="drf_1",
    body="Hi Haley, John is available (in ET): Mon 3/2: 8am-11am.",
):
    return {
        "id": draft_id,
        "suggestion_id": "sug_1",
        "loop_id": "lop_1",
        "coordinator_email": "fiona@lrp.com",
        "to_emails": ["haley@client.com"],
        "cc_emails": [],
        "subject": "Re: Round 1 - John Smith",
        "body": body,
        "gmail_thread_id": None,
        "is_forward": False,
        "status": "generated",
        "sent_at": None,
        "created_at": datetime(2026, 4, 15, tzinfo=UTC),
        "updated_at": datetime(2026, 4, 15, tzinfo=UTC),
    }


class TestRowToDraft:
    def test_converts_dict_to_model(self):
        draft = _row_to_draft(_draft_row())
        assert draft.id == "drf_1"
        assert draft.suggestion_id == "sug_1"
        assert draft.to_emails == ["haley@client.com"]
        assert draft.status == DraftStatus.GENERATED
        assert "John is available" in draft.body


class TestResolveRecipients:
    """Recipient routing is driven by the agent's recipient_type, NOT loop state.

    State describes where the loop is in its lifecycle; recipient_type
    describes who the next email is for. They're independent — e.g. on a
    SCHEDULED loop the agent may legitimately want to message the recruiter
    to relay a confirmation. These tests assert that the routing honors the
    agent's decision regardless of state.
    """

    def test_recruiter_recipient_type_routes_to_recruiter(self):
        loop = _loop(state=StageState.AWAITING_CANDIDATE)
        to, cc = resolve_recipients(loop, "recruiter")
        assert to == ["mike@recruiter.com"]
        assert cc == []

    def test_client_recipient_type_routes_to_client(self):
        loop = _loop(state=StageState.NEW)
        to, cc = resolve_recipients(loop, "client")
        assert to == ["haley@client.com"]
        assert cc == []

    def test_internal_recipient_type_yields_empty_to(self):
        """Internal notes have no external 'to' — only CC the CM if present."""
        loop = _loop(state=StageState.SCHEDULED, with_client_manager=True)
        to, cc = resolve_recipients(loop, "internal")
        assert to == []
        assert cc == ["sarah@lrp.com"]

    def test_state_does_not_override_recipient_type(self):
        """Regression: SCHEDULED loop + recipient_type='recruiter' → recruiter, not client."""
        loop = _loop(state=StageState.SCHEDULED)
        to, _ = resolve_recipients(loop, "recruiter")
        assert to == ["mike@recruiter.com"]

    def test_missing_recruiter_yields_empty_to(self):
        """If recipient_type='recruiter' but loop.recruiter is None, return empty
        to_emails so the JIT contact-collection path can prompt the coordinator
        instead of silently routing to the wrong person."""
        loop = _loop(state=StageState.NEW)
        loop.recruiter = None
        to, _ = resolve_recipients(loop, "recruiter")
        assert to == []

    def test_missing_client_yields_empty_to(self):
        loop = _loop(state=StageState.AWAITING_CLIENT)
        loop.client_contact = None
        to, _ = resolve_recipients(loop, "client")
        assert to == []

    def test_unknown_recipient_type_yields_empty_to(self):
        loop = _loop(state=StageState.NEW)
        to, _ = resolve_recipients(loop, None)
        assert to == []

    def test_client_manager_cc_when_present(self):
        loop = _loop(state=StageState.AWAITING_CANDIDATE, with_client_manager=True)
        to, cc = resolve_recipients(loop, "client")
        assert to == ["haley@client.com"]
        assert cc == ["sarah@lrp.com"]

    def test_sender_filtered_from_cc(self):
        """Coordinators are sometimes their own CM — don't CC yourself."""
        loop = _loop(state=StageState.AWAITING_CANDIDATE, with_client_manager=True)
        # sarah@lrp.com is the CM in the fixture
        _, cc = resolve_recipients(loop, "client", sender_email="sarah@lrp.com")
        assert cc == []


def _thread_msg(
    from_email: str,
    to_emails: list[str],
    cc_emails: list[str] | None = None,
    msg_id: str = "msg_1",
    date: datetime | None = None,
) -> Message:
    return Message(
        id=msg_id,
        thread_id="thread_1",
        subject="Interview",
        **{"from": EmailAddress(email=from_email)},
        to=[EmailAddress(email=e) for e in to_emails],
        cc=[EmailAddress(email=e) for e in (cc_emails or [])],
        date=date or datetime(2026, 4, 15, tzinfo=UTC),
        body_text="test",
    )


class TestIsForwardDraft:
    def test_new_recipient_is_forward(self):
        msgs = [_thread_msg("alice@a.com", ["coord@lrp.com"])]
        assert _is_forward_draft(["bob@b.com"], msgs) is True

    def test_existing_recipient_is_not_forward(self):
        msgs = [_thread_msg("alice@a.com", ["coord@lrp.com"])]
        assert _is_forward_draft(["alice@a.com"], msgs) is False

    def test_no_thread_messages_is_not_forward(self):
        assert _is_forward_draft(["anyone@a.com"], None) is False
        assert _is_forward_draft(["anyone@a.com"], []) is False

    def test_only_checks_trigger_message(self):
        """Recipient on an earlier message but NOT on the trigger → forward."""
        old = _thread_msg(
            "bob@b.com",
            ["coord@lrp.com"],
            msg_id="msg_old",
            date=datetime(2026, 4, 14, tzinfo=UTC),
        )
        trigger = _thread_msg(
            "alice@a.com",
            ["coord@lrp.com"],
            msg_id="msg_new",
            date=datetime(2026, 4, 15, tzinfo=UTC),
        )
        assert _is_forward_draft(["bob@b.com"], [old, trigger], "msg_new") is True

    def test_recipient_on_trigger_is_not_forward(self):
        """Recipient on the trigger message → not a forward."""
        trigger = _thread_msg(
            "alice@a.com",
            ["bob@b.com", "coord@lrp.com"],
            msg_id="msg_new",
        )
        assert _is_forward_draft(["bob@b.com"], [trigger], "msg_new") is False

    def test_client_recipient_never_forwards_even_when_not_on_trigger(self):
        """Privacy guard: recipient_type='client' MUST short-circuit to False.

        Scenario that motivated this: client → coord → coord forwards to
        recruiter → recruiter replies → agent suggests drafting back to
        client. The trigger is the recruiter's reply, which the client
        isn't on. Without this guard, _is_forward_draft would return True
        and the recruiter's reply would be quoted to the client (LEAK).
        """
        recruiter_reply = _thread_msg(
            "recruiter@lrp.com",
            ["coord@lrp.com"],
            msg_id="msg_recruiter",
        )
        assert (
            _is_forward_draft(
                ["client@external.com"],
                [recruiter_reply],
                "msg_recruiter",
                recipient_type="client",
            )
            is False
        )

    def test_client_recipient_returns_false_even_when_on_trigger(self):
        """Consistency: client recipient is always reply, never forward."""
        trigger = _thread_msg(
            "alice@a.com",
            ["client@external.com", "coord@lrp.com"],
            msg_id="msg_new",
        )
        assert (
            _is_forward_draft(
                ["client@external.com"], [trigger], "msg_new", recipient_type="client"
            )
            is False
        )

    def test_recruiter_recipient_keeps_trigger_based_detection(self):
        """Non-client recipient types retain existing behavior — forwarding
        to internal LRP folks is safe and often correct."""
        client_msg = _thread_msg(
            "client@external.com",
            ["coord@lrp.com"],
            msg_id="msg_client",
        )
        # Recruiter not on the trigger → forward (preserves existing semantics).
        assert (
            _is_forward_draft(
                ["recruiter@lrp.com"],
                [client_msg],
                "msg_client",
                recipient_type="recruiter",
            )
            is True
        )

    def test_recipient_type_none_legacy_keeps_trigger_based_detection(self):
        """recipient_type=None (legacy suggestions) → trigger-based detection."""
        client_msg = _thread_msg(
            "client@external.com",
            ["coord@lrp.com"],
            msg_id="msg_client",
        )
        assert (
            _is_forward_draft(
                ["new@elsewhere.com"],
                [client_msg],
                "msg_client",
                recipient_type=None,
            )
            is True
        )


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


def _mock_pool():
    mock_conn = MagicMock()
    mock_pool = MagicMock()
    mock_pool.connection.return_value = _AsyncCtx(mock_conn)
    mock_conn.transaction.return_value = _AsyncCtx(None)
    return mock_pool, mock_conn


class TestResolveSubject:
    """Subject derivation for outbound replies.

    Gmail's threading rules (developers.google.com/gmail/api/guides/threads)
    require the outbound Subject to match the existing thread's Subject for
    the reply to attach. The function under test mirrors the latest thread
    message's subject rather than synthesizing from loop.title.
    """

    def test_mirrors_latest_thread_subject(self):
        msg = _thread_msg("alice@a.com", ["coord@lrp.com"])
        msg.subject = "Trump"
        result = DraftService._resolve_subject(_loop(), [msg])
        assert result == "Re: Trump"

    def test_no_double_re_prefix_lowercase(self):
        msg = _thread_msg("alice@a.com", ["coord@lrp.com"])
        msg.subject = "re: Phone screen"
        result = DraftService._resolve_subject(_loop(), [msg])
        assert result == "re: Phone screen"

    def test_no_double_re_prefix_uppercase(self):
        msg = _thread_msg("alice@a.com", ["coord@lrp.com"])
        msg.subject = "RE: Phone screen"
        result = DraftService._resolve_subject(_loop(), [msg])
        assert result == "RE: Phone screen"

    def test_no_double_re_prefix_titlecase(self):
        msg = _thread_msg("alice@a.com", ["coord@lrp.com"])
        msg.subject = "Re: Phone screen"
        result = DraftService._resolve_subject(_loop(), [msg])
        assert result == "Re: Phone screen"

    def test_falls_back_to_loop_title_when_no_thread(self):
        result = DraftService._resolve_subject(_loop(), None)
        assert result == "Re: Round 1 - John Smith"

    def test_falls_back_to_loop_title_when_thread_empty(self):
        result = DraftService._resolve_subject(_loop(), [])
        assert result == "Re: Round 1 - John Smith"

    def test_picks_latest_by_date_not_list_order(self):
        """The latest message wins even when list order is oldest-first."""
        oldest = _thread_msg(
            "alice@a.com",
            ["coord@lrp.com"],
            msg_id="msg_old",
            date=datetime(2026, 4, 14, tzinfo=UTC),
        )
        oldest.subject = "Re: Old subject"
        newest = _thread_msg(
            "alice@a.com",
            ["coord@lrp.com"],
            msg_id="msg_new",
            date=datetime(2026, 4, 16, tzinfo=UTC),
        )
        newest.subject = "Trump"
        result = DraftService._resolve_subject(_loop(), [oldest, newest])
        assert result == "Re: Trump"

    def test_falls_back_when_latest_subject_is_blank(self):
        """Defensive: a thread message with whitespace-only subject falls
        back to the loop title rather than producing 'Re: ' alone."""
        msg = _thread_msg("alice@a.com", ["coord@lrp.com"])
        msg.subject = "   "
        result = DraftService._resolve_subject(_loop(), [msg])
        assert result == "Re: Round 1 - John Smith"


class TestGenerateDraft:
    @pytest.mark.asyncio
    async def test_body_persisted(self):
        mock_pool, _ = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        loop = _loop(state=StageState.AWAITING_CANDIDATE)
        suggestion = _suggestion()
        body = "Hi Haley, John is available (in ET): Mon 3/2: 8am-11am."

        from unittest.mock import patch

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.create_draft = AsyncMock(return_value=_draft_row(body=body))
            draft = await svc.generate_draft(suggestion=suggestion, loop=loop, body=body)
            assert draft.body == body
            assert draft.to_emails == ["haley@client.com"]
            mock_queries.create_draft.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_body_creates_draft(self):
        mock_pool, _ = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        loop = _loop(state=StageState.AWAITING_CANDIDATE)
        suggestion = _suggestion()

        from unittest.mock import patch

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.create_draft = AsyncMock(return_value=_draft_row(body=""))
            draft = await svc.generate_draft(suggestion=suggestion, loop=loop, body="")
            assert draft.body == ""

    @pytest.mark.asyncio
    async def test_subject_mirrors_thread_subject_not_loop_title(self):
        """Regression: the persisted draft subject must come from the thread
        (Gmail threading requires Subject match), not from loop.title."""
        mock_pool, _ = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        loop = _loop(state=StageState.AWAITING_CANDIDATE)
        suggestion = _suggestion()
        thread_msg = _thread_msg("alice@client.com", ["coord@lrp.com"])
        thread_msg.subject = "Trump"

        from unittest.mock import patch

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.create_draft = AsyncMock(return_value=_draft_row(body=""))
            await svc.generate_draft(
                suggestion=suggestion,
                loop=loop,
                thread_messages=[thread_msg],
                body="",
            )
            call_kwargs = mock_queries.create_draft.await_args.kwargs
            # Subject derived from thread, not from loop.title ("Round 1 - John Smith").
            assert call_kwargs["subject"] == "Re: Trump"


class TestDraftLifecycle:
    @pytest.mark.asyncio
    async def test_mark_sent(self):
        mock_pool, mock_conn = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        from unittest.mock import patch

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.mark_draft_sent = AsyncMock()
            await svc.mark_sent("drf_1")
            mock_queries.mark_draft_sent.assert_awaited_once_with(mock_conn, id="drf_1")

    @pytest.mark.asyncio
    async def test_update_draft_body(self):
        mock_pool, mock_conn = _mock_pool()
        svc = DraftService(db_pool=mock_pool, loop_service=MagicMock())

        from unittest.mock import patch

        with patch("api.drafts.service.queries") as mock_queries:
            mock_queries.update_draft_body = AsyncMock()
            await svc.update_draft_body("drf_1", "Updated body")
            mock_queries.update_draft_body.assert_awaited_once_with(
                mock_conn, id="drf_1", body="Updated body"
            )
