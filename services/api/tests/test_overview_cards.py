"""Tests for the suggestion-centric overview card builders."""

from datetime import UTC, datetime

from api.addon.models import CardResponse
from api.classifier.models import (
    EmailClassification,
    SuggestedAction,
    Suggestion,
    SuggestionStatus,
)
from api.drafts.models import DraftStatus, EmailDraft
from api.overview.cards import (
    _build_advance_suggestion,
    _build_ask_suggestion,
    _build_create_loop_suggestion,
    _build_draft_suggestion,
    _build_link_thread_suggestion,
    _build_suggestion_widgets,
    build_overview,
)
from api.overview.models import LoopSuggestionGroup, SuggestionView


def _suggestion(
    action: SuggestedAction = SuggestedAction.ADVANCE_STAGE,
    suggestion_id: str = "sug_1",
    loop_id: str | None = "lop_1",
    summary: str = "Advance to Awaiting Client",
    reasoning: str | None = None,
    action_data: dict | None = None,
) -> Suggestion:
    return Suggestion(
        id=suggestion_id,
        coordinator_email="fiona@lrp.com",
        gmail_message_id="msg_1",
        gmail_thread_id="thread_1",
        loop_id=loop_id,
        classification=EmailClassification.AVAILABILITY_RESPONSE,
        action=action,
        confidence=0.9,
        summary=summary,
        action_data=action_data or {},
        reasoning=reasoning,
        status=SuggestionStatus.PENDING,
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


def _draft(suggestion_id: str = "sug_1") -> EmailDraft:
    return EmailDraft(
        id="drf_1",
        suggestion_id=suggestion_id,
        loop_id="lop_1",
        coordinator_email="fiona@lrp.com",
        to_emails=["haley@client.com"],
        cc_emails=["bob@client.com"],
        subject="Re: Round 1 - Jane Doe",
        body="Hi Haley, Jane is available Mon 3/2 8am-11am.",
        status=DraftStatus.GENERATED,
        gmail_thread_id="thread_1",
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
        updated_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


def _view(
    action: SuggestedAction = SuggestedAction.ADVANCE_STAGE,
    loop_title: str | None = "Jane Doe, ACME Corp",
    loop_state: str | None = "awaiting_candidate",
    candidate_name: str | None = "Jane Doe",
    client_company: str | None = "ACME Corp",
    draft: EmailDraft | None = None,
    **kwargs,
) -> SuggestionView:
    return SuggestionView(
        suggestion=_suggestion(action=action, **kwargs),
        loop_title=loop_title,
        loop_state=loop_state,
        candidate_name=candidate_name,
        client_company=client_company,
        draft=draft,
    )


def _group(
    views: list[SuggestionView] | None = None,
    loop_id: str | None = "lop_1",
    loop_title: str | None = "Jane Doe, ACME Corp",
) -> LoopSuggestionGroup:
    if views is None:
        views = [_view()]
    return LoopSuggestionGroup(
        loop_id=loop_id,
        loop_title=loop_title,
        candidate_name="Jane Doe",
        client_company="ACME Corp",
        suggestions=views,
        oldest_created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


class TestBuildOverview:
    def test_empty_groups_shows_empty_state(self):
        card = build_overview([])
        assert isinstance(card, CardResponse)
        serialized = card.model_dump(by_alias=True, exclude_none=True)
        text = str(serialized)
        assert "caught up" in text.lower()

    def test_single_group_renders(self):
        card = build_overview([_group()])
        assert isinstance(card, CardResponse)
        serialized = card.model_dump(by_alias=True, exclude_none=True)
        sections = serialized["action"]["navigations"][0]["updateCard"]["sections"]
        assert len(sections) >= 1


class TestDraftSuggestionBuilder:
    def test_with_draft_shows_body_input(self):
        view = _view(
            action=SuggestedAction.DRAFT_EMAIL,
            summary="Share availability with ACME",
            draft=_draft(),
            action_data={"body": "Share availability", "recipient_type": "client"},
        )
        widgets = _build_draft_suggestion(view)
        assert len(widgets) >= 6
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "draft_body_sug_1" in text

    def test_without_draft_shows_placeholder(self):
        view = _view(
            action=SuggestedAction.DRAFT_EMAIL,
            summary="Share availability",
            action_data={"body": "Share availability", "recipient_type": "client"},
        )
        widgets = _build_draft_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "being generated" in text.lower()


class TestCreateLoopSuggestionBuilder:
    def test_prefilled_from_action_data(self):
        view = _view(
            action=SuggestedAction.CREATE_LOOP,
            loop_id=None,
            loop_title=None,
            summary="New interview request detected",
            action_data={
                "candidate_name": "Adam L'esperance",
                "client_email": "nim@kinematiclabs.dev",
            },
        )
        widgets = _build_create_loop_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Adam L'esperance" in text
        assert "Create Loop" in text


class TestAdvanceSuggestionBuilder:
    def test_with_loop_state_shows_from_to(self):
        view = _view(
            action=SuggestedAction.ADVANCE_STAGE,
            summary="Advance to Awaiting Client",
            loop_state="awaiting_candidate",
            action_data={"target_stage": "awaiting_client"},
        )
        widgets = _build_advance_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Awaiting Candidate" in text
        assert "Awaiting Client" in text


class TestLinkThreadSuggestionBuilder:
    def test_shows_reasoning(self):
        view = _view(
            action=SuggestedAction.LINK_THREAD,
            summary="Link to Jane Doe loop",
            reasoning="Thread mentions ACME and candidate availability",
        )
        widgets = _build_link_thread_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "ACME" in text
        assert "Link" in text


class TestAskCoordinatorSuggestionBuilder:
    def test_shows_question_from_action_data(self):
        view = _view(
            action=SuggestedAction.ASK_COORDINATOR,
            summary="Agent needs clarification",
            action_data={"question": "Should we propose morning or afternoon slots?"},
        )
        widgets = _build_ask_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "morning or afternoon" in text
        assert "respond_to_question" in text
        assert "coordinator_response_sug_1" in text


class TestSuggestionDispatcher:
    def test_dispatches_all_known_types(self):
        for action in [
            SuggestedAction.ADVANCE_STAGE,
            SuggestedAction.DRAFT_EMAIL,
            SuggestedAction.ASK_COORDINATOR,
            SuggestedAction.LINK_THREAD,
            SuggestedAction.CREATE_LOOP,
        ]:
            view = _view(action=action)
            widgets = _build_suggestion_widgets(view)
            assert len(widgets) > 0, f"No widgets for {action}"
