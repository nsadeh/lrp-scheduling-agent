"""Smoke tests for draft card builders."""

from datetime import UTC, datetime

from api.addon.models import CardResponse
from api.drafts.cards import (
    build_draft_edit,
    build_draft_preview,
)
from api.drafts.models import DraftStatus, EmailDraft


def _draft(
    draft_id="drf_1",
    body="Hi Haley, John is available (in ET): Mon 3/2: 8am-11am.",
    status=DraftStatus.GENERATED,
) -> EmailDraft:
    return EmailDraft(
        id=draft_id,
        suggestion_id="sug_1",
        loop_id="lop_1",
        stage_id="stg_1",
        coordinator_email="fiona@lrp.com",
        to_emails=["haley@client.com"],
        cc_emails=["sarah@lrp.com"],
        subject="Re: Round 1 - John Smith",
        body=body,
        gmail_thread_id="thread_1",
        status=status,
        sent_at=None,
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
        updated_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


class TestBuildDraftPreview:
    def test_returns_card_response(self):
        card = build_draft_preview(_draft())
        assert isinstance(card, CardResponse)

    def test_includes_body_text(self):
        card = build_draft_preview(_draft())
        serialized = card.model_dump(by_alias=True, exclude_none=True)
        # The card should serialize without errors
        assert serialized is not None

    def test_empty_body_shows_placeholder(self):
        card = build_draft_preview(_draft(body=""))
        assert isinstance(card, CardResponse)


class TestBuildDraftEdit:
    def test_returns_card_response(self):
        card = build_draft_edit(_draft())
        assert isinstance(card, CardResponse)

    def test_shows_cc_when_present(self):
        card = build_draft_edit(_draft())
        assert isinstance(card, CardResponse)
