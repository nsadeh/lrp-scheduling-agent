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
