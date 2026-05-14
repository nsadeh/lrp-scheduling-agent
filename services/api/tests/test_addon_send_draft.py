"""Tests for _handle_send_draft's forward behavior (issue #36).

The draft-send path is where quoted-history injection happens on the wire. These
tests verify that:
  - `is_forward=True` drafts get the prior thread quoted into the body and a
    "Fwd:" subject prefix on send.
  - `is_forward=False` drafts (replies) are sent unchanged — no quote, no prefix.
  - A forward whose thread fetch fails raises instead of silently sending a
    context-less note.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.addon.models import AddonRequest, CommonEventObject
from api.addon.routes import _handle_send_draft
from api.drafts.models import DraftStatus, EmailDraft
from api.gmail.models import EmailAddress, Message, Thread


def _make_message(
    *,
    msg_id: str = "m1",
    from_email: str = "alice@client.com",
    from_name: str | None = "Alice Client",
    body_text: str = "Original ask: share availability for Claire.",
    subject: str = "Phone screen for Claire Cao",
    message_id_header: str | None = "<m1@mail.gmail.com>",
) -> Message:
    return Message(
        id=msg_id,
        thread_id="t1",
        subject=subject,
        **{"from": EmailAddress(name=from_name, email=from_email)},
        to=[EmailAddress(name="Coord", email="coord@longridgepartners.com")],
        cc=[],
        date=datetime(2026, 4, 20, 9, 42, tzinfo=UTC),
        body_text=body_text,
        message_id_header=message_id_header,
    )


def _make_draft(*, is_forward: bool, draft_id: str = "drft_1") -> EmailDraft:
    return EmailDraft(
        id=draft_id,
        suggestion_id="sug_1",
        loop_id="lup_1",
        stage_id="stg_1",
        coordinator_email="coord@longridgepartners.com",
        to_emails=["recruiter@external.com"] if is_forward else ["alice@client.com"],
        cc_emails=[],
        subject="Phone screen for Claire Cao",
        body="Please share availability.",
        gmail_thread_id="t1",
        is_forward=is_forward,
        status=DraftStatus.GENERATED,
    )


def _build_context(*, thread: Thread | None, thread_fetch_raises: bool = False):
    """Build (body, svc, email, request_ctx, draft_svc, expected_pool) for the call."""
    body = AddonRequest(common_event_object=CommonEventObject(parameters={"draft_id": "drft_1"}))

    draft_svc = SimpleNamespace(
        get_draft=AsyncMock(),  # test sets .return_value
        update_draft_body=AsyncMock(),
        mark_sent=AsyncMock(),
    )

    gmail = SimpleNamespace()
    if thread_fetch_raises:
        gmail.get_thread = AsyncMock(side_effect=RuntimeError("gmail down"))
    else:
        gmail.get_thread = AsyncMock(return_value=thread)

    app_state = SimpleNamespace(
        draft_service=draft_svc,
        gmail=gmail,
        overview_service=SimpleNamespace(),  # not used because we patch _build_refreshed_overview
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    # _pool is only read to build a SuggestionService, which we patch.
    svc = SimpleNamespace(send_email=AsyncMock(), _pool=SimpleNamespace())

    return body, svc, "coord@longridgepartners.com", request, draft_svc


@pytest.mark.asyncio
async def test_forward_draft_quotes_thread_and_prefixes_subject():
    thread = Thread(id="t1", messages=[_make_message()])
    body, svc, email, request, draft_svc = _build_context(thread=thread)
    draft = _make_draft(is_forward=True)
    draft_svc.get_draft.return_value = draft

    with (
        patch("api.classifier.service.SuggestionService") as sug_cls,
        patch("api.addon.routes._build_refreshed_overview", new=AsyncMock(return_value=None)),
    ):
        sug_cls.return_value.resolve = AsyncMock()
        await _handle_send_draft(body, svc, email, request=request)

    svc.send_email.assert_awaited_once()
    call = svc.send_email.await_args.kwargs
    assert call["subject"] == "Fwd: Phone screen for Claire Cao"
    assert call["body"].startswith("Please share availability.\n\n")
    assert "---------- Forwarded message ----------" in call["body"]
    assert "From: Alice Client <alice@client.com>" in call["body"]
    assert "Original ask: share availability for Claire." in call["body"]
    # Threading headers still wired up for same-thread display when possible.
    assert call["in_reply_to"] == "<m1@mail.gmail.com>"
    assert call["references"] == "<m1@mail.gmail.com>"


@pytest.mark.asyncio
async def test_reply_draft_is_sent_unchanged():
    thread = Thread(id="t1", messages=[_make_message()])
    body, svc, email, request, draft_svc = _build_context(thread=thread)
    draft = _make_draft(is_forward=False)
    draft_svc.get_draft.return_value = draft

    with (
        patch("api.classifier.service.SuggestionService") as sug_cls,
        patch("api.addon.routes._build_refreshed_overview", new=AsyncMock(return_value=None)),
    ):
        sug_cls.return_value.resolve = AsyncMock()
        await _handle_send_draft(body, svc, email, request=request)

    call = svc.send_email.await_args.kwargs
    # Subject untouched — no Fwd: prefix.
    assert call["subject"] == "Phone screen for Claire Cao"
    # Body is exactly the draft body — no quoted history appended.
    assert call["body"] == "Please share availability."
    assert "Forwarded message" not in call["body"]


@pytest.mark.asyncio
async def test_forward_raises_when_thread_fetch_fails():
    """A forward without its quoted history is actively harmful — fail loudly."""
    body, svc, email, request, draft_svc = _build_context(thread=None, thread_fetch_raises=True)
    draft = _make_draft(is_forward=True)
    draft_svc.get_draft.return_value = draft

    with (
        patch("api.classifier.service.SuggestionService"),
        patch("api.addon.routes._build_refreshed_overview", new=AsyncMock(return_value=None)),
        pytest.raises(RuntimeError),
    ):
        await _handle_send_draft(body, svc, email, request=request)

    svc.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_still_sends_when_thread_fetch_fails():
    """Regression guard: replies keep the soft-fallback behavior."""
    body, svc, email, request, draft_svc = _build_context(thread=None, thread_fetch_raises=True)
    draft = _make_draft(is_forward=False)
    draft_svc.get_draft.return_value = draft

    with (
        patch("api.classifier.service.SuggestionService") as sug_cls,
        patch("api.addon.routes._build_refreshed_overview", new=AsyncMock(return_value=None)),
    ):
        sug_cls.return_value.resolve = AsyncMock()
        await _handle_send_draft(body, svc, email, request=request)

    # Sent despite thread fetch failure, but without threading headers.
    call = svc.send_email.await_args.kwargs
    assert call["in_reply_to"] is None
    assert call["references"] is None
    assert call["body"] == "Please share availability."


def _build_jit_context(*, draft: EmailDraft):
    """Like _build_context but routes through the suggestion_id-bearing form
    that _handle_send_draft uses to decide whether to run _apply_jit_contacts.
    """
    body = AddonRequest(
        common_event_object=CommonEventObject(
            parameters={"draft_id": draft.id, "suggestion_id": draft.suggestion_id}
        )
    )
    draft_svc = SimpleNamespace(
        get_draft=AsyncMock(return_value=draft),
        update_draft_body=AsyncMock(),
        mark_sent=AsyncMock(),
    )
    gmail = SimpleNamespace(get_thread=AsyncMock(return_value=None))
    app_state = SimpleNamespace(
        draft_service=draft_svc,
        gmail=gmail,
        overview_service=SimpleNamespace(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    svc = SimpleNamespace(send_email=AsyncMock(), _pool=SimpleNamespace())
    return body, svc, "coord@longridgepartners.com", request, draft_svc


@pytest.mark.asyncio
async def test_jit_apply_runs_when_pending_data_present_even_with_to_emails():
    """Bug fix: a staged CM (in pending_jit_data) must be committed at send,
    even when the loop already has a recruiter (so draft.to_emails is set).
    Before this fix, the `not draft.to_emails` guard short-circuited the
    JIT-apply step for CM-only stagings."""
    draft = _make_draft(is_forward=False)  # to_emails=["alice@client.com"]
    draft.pending_jit_data = {"client_manager": {"name": "Carla M", "email": "cm@client.com"}}
    body, svc, email, request, _draft_svc = _build_jit_context(draft=draft)

    with (
        patch(
            "api.addon.routes._apply_jit_contacts",
            new=AsyncMock(return_value=draft),
        ) as apply_jit,
        patch("api.classifier.service.SuggestionService") as sug_cls,
        patch("api.addon.routes._build_refreshed_overview", new=AsyncMock(return_value=None)),
    ):
        sug_cls.return_value.resolve = AsyncMock()
        await _handle_send_draft(body, svc, email, request=request)

    apply_jit.assert_awaited_once()
    svc.send_email.assert_awaited_once()


@pytest.mark.asyncio
async def test_jit_apply_skipped_when_nothing_pending_and_recipients_present():
    """Counterpart: when the loop is fully populated and nothing is staged,
    we should NOT pay the cost of _apply_jit_contacts on send."""
    draft = _make_draft(is_forward=False)
    draft.pending_jit_data = {}
    body, svc, email, request, _draft_svc = _build_jit_context(draft=draft)

    with (
        patch(
            "api.addon.routes._apply_jit_contacts",
            new=AsyncMock(return_value=draft),
        ) as apply_jit,
        patch("api.classifier.service.SuggestionService") as sug_cls,
        patch("api.addon.routes._build_refreshed_overview", new=AsyncMock(return_value=None)),
    ):
        sug_cls.return_value.resolve = AsyncMock()
        await _handle_send_draft(body, svc, email, request=request)

    apply_jit.assert_not_awaited()
    svc.send_email.assert_awaited_once()


@pytest.mark.asyncio
async def test_jit_apply_runs_when_to_emails_missing_regression():
    """Regression guard: the original use case (recruiter/client missing,
    to_emails empty, JIT inputs in form fields) still routes through
    _apply_jit_contacts."""
    draft = _make_draft(is_forward=False)
    draft.to_emails = []
    draft.pending_jit_data = {}
    body, svc, email, request, _draft_svc = _build_jit_context(draft=draft)

    # Return a draft with recipients populated to simulate a successful JIT fill.
    filled = _make_draft(is_forward=False)
    with (
        patch(
            "api.addon.routes._apply_jit_contacts",
            new=AsyncMock(return_value=filled),
        ) as apply_jit,
        patch("api.classifier.service.SuggestionService") as sug_cls,
        patch("api.addon.routes._build_refreshed_overview", new=AsyncMock(return_value=None)),
    ):
        sug_cls.return_value.resolve = AsyncMock()
        await _handle_send_draft(body, svc, email, request=request)

    apply_jit.assert_awaited_once()
    svc.send_email.assert_awaited_once()


class TestPickThreadAnchor:
    """_pick_thread_anchor decides which Message-ID we point In-Reply-To at."""

    def test_no_messages_returns_none(self):
        from api.addon.routes import _pick_thread_anchor

        assert _pick_thread_anchor([], ["x@y.com"], is_forward=False) is None

    def test_forward_anchors_on_latest_overall(self):
        """Forwards introduce a new party who hasn't sent anything yet —
        anchor on the latest message regardless of recipient match."""
        from api.addon.routes import _pick_thread_anchor

        older = _make_message(
            msg_id="m_old",
            from_email="alice@client.com",
            message_id_header="<old@mail.gmail.com>",
        )
        older.date = datetime(2026, 4, 1, tzinfo=UTC)
        latest = _make_message(
            msg_id="m_new",
            from_email="recruiter@lrp.com",
            message_id_header="<new@mail.gmail.com>",
        )
        latest.date = datetime(2026, 4, 20, tzinfo=UTC)
        anchor = _pick_thread_anchor([older, latest], ["bob@new-party.com"], is_forward=True)
        assert anchor.message_id_header == "<new@mail.gmail.com>"

    def test_reply_to_client_with_prior_send_anchors_on_client(self):
        """Client recipient who has sent on this thread → anchor on their
        latest send so their Gmail can thread by Message-ID match."""
        from api.addon.routes import _pick_thread_anchor

        client_msg = _make_message(
            msg_id="m_client",
            from_email="alice@client.com",
            message_id_header="<client@mail.gmail.com>",
        )
        client_msg.date = datetime(2026, 4, 10, tzinfo=UTC)
        recruiter_reply = _make_message(
            msg_id="m_rec",
            from_email="recruiter@lrp.com",
            message_id_header="<recruiter@mail.gmail.com>",
        )
        recruiter_reply.date = datetime(2026, 4, 15, tzinfo=UTC)
        anchor = _pick_thread_anchor(
            [client_msg, recruiter_reply], ["alice@client.com"], is_forward=False
        )
        assert anchor.message_id_header == "<client@mail.gmail.com>"

    def test_reply_to_recipient_not_in_thread_falls_back_to_latest(self):
        """When the recipient hasn't sent anything on this thread (the
        coord-forwarded-into-recruiter-only-thread scenario), fall back to
        the latest message overall. Subject mirroring carries threading
        on the recipient's side; this preserves prior behavior."""
        from api.addon.routes import _pick_thread_anchor

        coord_fwd = _make_message(
            msg_id="m_fwd",
            from_email="coord@lrp.com",
            message_id_header="<fwd@mail.gmail.com>",
        )
        coord_fwd.date = datetime(2026, 4, 10, tzinfo=UTC)
        recruiter_reply = _make_message(
            msg_id="m_rec",
            from_email="recruiter@lrp.com",
            message_id_header="<recruiter@mail.gmail.com>",
        )
        recruiter_reply.date = datetime(2026, 4, 15, tzinfo=UTC)
        # Client has never sent on this thread.
        anchor = _pick_thread_anchor(
            [coord_fwd, recruiter_reply], ["alice@client.com"], is_forward=False
        )
        assert anchor.message_id_header == "<recruiter@mail.gmail.com>"

    def test_reply_picks_latest_match_when_multiple_from_recipient(self):
        """If the recipient has sent more than once, pick their LATEST."""
        from api.addon.routes import _pick_thread_anchor

        first = _make_message(
            msg_id="m_first",
            from_email="alice@client.com",
            message_id_header="<first@mail.gmail.com>",
        )
        first.date = datetime(2026, 4, 1, tzinfo=UTC)
        second = _make_message(
            msg_id="m_second",
            from_email="alice@client.com",
            message_id_header="<second@mail.gmail.com>",
        )
        second.date = datetime(2026, 4, 20, tzinfo=UTC)
        # Intentionally not in chronological order:
        anchor = _pick_thread_anchor([second, first], ["alice@client.com"], is_forward=False)
        assert anchor.message_id_header == "<second@mail.gmail.com>"

    def test_reply_recipient_match_is_case_insensitive(self):
        from api.addon.routes import _pick_thread_anchor

        client_msg = _make_message(
            msg_id="m_client",
            from_email="Alice@Client.com",
            message_id_header="<client@mail.gmail.com>",
        )
        anchor = _pick_thread_anchor([client_msg], ["ALICE@CLIENT.COM"], is_forward=False)
        assert anchor.message_id_header == "<client@mail.gmail.com>"
