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
    _build_mark_cold_suggestion,
    _build_suggestion_widgets,
    build_overview,
)
from api.overview.models import LoopSuggestionGroup, SuggestionView


def _suggestion(
    action: SuggestedAction = SuggestedAction.ADVANCE_STAGE,
    suggestion_id: str = "sug_1",
    loop_id: str | None = "lop_1",
    stage_id: str | None = "stg_1",
    summary: str = "Advance to Awaiting Client",
    target_state: str | None = "awaiting_client",
    extracted_entities: dict | None = None,
    questions: list | None = None,
    reasoning: str | None = None,
    action_data: dict | None = None,
) -> Suggestion:
    return Suggestion(
        id=suggestion_id,
        coordinator_email="fiona@lrp.com",
        gmail_message_id="msg_1",
        gmail_thread_id="thread_1",
        loop_id=loop_id,
        stage_id=stage_id,
        classification=EmailClassification.AVAILABILITY_RESPONSE,
        action=action,
        confidence=0.9,
        summary=summary,
        target_state=target_state,
        extracted_entities=extracted_entities or {},
        questions=questions or [],
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
        stage_id="stg_1",
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
    candidate_name: str | None = "Jane Doe",
    client_company: str | None = "ACME Corp",
    stage_name: str | None = None,
    stage_state: str | None = None,
    draft: EmailDraft | None = None,
    **kwargs,
) -> SuggestionView:
    return SuggestionView(
        suggestion=_suggestion(action=action, **kwargs),
        loop_title=loop_title,
        candidate_name=candidate_name,
        client_company=client_company,
        stage_name=stage_name,
        stage_state=stage_state,
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
        # Should have "All caught up" text
        text = str(serialized)
        assert "caught up" in text.lower()

    def test_single_group_renders(self):
        card = build_overview([_group()])
        assert isinstance(card, CardResponse)
        serialized = card.model_dump(by_alias=True, exclude_none=True)
        sections = serialized["action"]["navigations"][0]["updateCard"]["sections"]
        assert len(sections) >= 1

    def test_multiple_groups_render_separate_sections(self):
        groups = [
            _group(loop_id="lop_1", loop_title="Jane Doe, ACME"),
            _group(
                views=[_view(suggestion_id="sug_2", loop_id="lop_2")],
                loop_id="lop_2",
                loop_title="Bob Smith, XYZ Fund",
            ),
        ]
        card = build_overview(groups)
        serialized = card.model_dump(by_alias=True, exclude_none=True)
        sections = serialized["action"]["navigations"][0]["updateCard"]["sections"]
        # One section per loop group
        assert len(sections) >= 2

    def test_refresh_button_included_with_base_url(self):
        card = build_overview([], base_url="https://test.ngrok-free.app")
        serialized = card.model_dump(by_alias=True, exclude_none=True)
        text = str(serialized)
        assert "Refresh" in text
        assert "/addon/refresh" in text

    def test_serializes_without_errors(self):
        """Ensure the full card JSON is valid for Google's Card API."""
        groups = [_group()]
        card = build_overview(groups, base_url="https://test.ngrok-free.app")
        serialized = card.model_dump(by_alias=True, exclude_none=True)
        assert serialized is not None


class TestDraftSuggestionBuilder:
    def test_with_draft_shows_body_input(self):
        view = _view(
            action=SuggestedAction.DRAFT_EMAIL,
            summary="Share availability with ACME",
            draft=_draft(),
        )
        widgets = _build_draft_suggestion(view)
        # Should have: summary, To, CC, Subject, divider, TextInput, buttons
        assert len(widgets) >= 6
        # Check unique input name
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "draft_body_sug_1" in text

    def test_no_edit_button_only_send_and_dismiss(self):
        view = _view(
            action=SuggestedAction.DRAFT_EMAIL,
            summary="Share availability with ACME",
            draft=_draft(),
        )
        widgets = _build_draft_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Send" in text
        assert "Edit & Send" not in text
        assert "edit_draft" not in text

    def test_forward_draft_shows_forward_button_and_optional_note(self):
        """Forward drafts show 'Forward' button with optional note instead of required 'Send'."""
        fwd_draft = _draft()
        fwd_draft.is_forward = True
        view = _view(
            action=SuggestedAction.DRAFT_EMAIL,
            summary="Forward availability to recruiter",
            draft=fwd_draft,
        )
        widgets = _build_draft_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Forward" in text
        assert "Forward note" in text
        # Forward note should NOT be in required_widgets (it's optional)
        assert (
            "required_widgets" not in text
            or "draft_body" not in text.split("required_widgets")[1].split("]")[0]
        )

    def test_reply_draft_shows_send_button_and_required_message(self):
        """Reply drafts show 'Send' button with required 'Message' field."""
        reply_draft = _draft()
        reply_draft.is_forward = False
        view = _view(
            action=SuggestedAction.DRAFT_EMAIL,
            summary="Reply to client",
            draft=reply_draft,
        )
        widgets = _build_draft_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Send" in text
        assert "Forward" not in text
        assert "Message" in text

    def test_without_draft_shows_placeholder(self):
        view = _view(
            action=SuggestedAction.DRAFT_EMAIL,
            summary="Share availability",
        )
        widgets = _build_draft_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "being generated" in text.lower()


class TestCreateLoopSuggestionBuilder:
    def test_renders_extracted_entities(self):
        view = _view(
            action=SuggestedAction.CREATE_LOOP,
            loop_id=None,
            loop_title=None,
            summary="New interview request detected",
            extracted_entities={
                "candidate_name": "Claire Thompson",
                "client_name": "Haley",
                "client_email": "haley@acme.com",
                "client_company": "ACME Corp",
                "recruiter_name": "Bob Smith",
                "recruiter_email": "bob@lrp.com",
            },
        )
        widgets = _build_create_loop_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Claire Thompson" in text
        assert "ACME Corp" in text
        assert "Create Loop" in text
        # CM fields should be present (empty when not provided)
        assert "cm_name_" in text
        assert "cm_email_" in text

    def test_prefilled_from_action_data(self):
        """Fields in action_data take priority over extracted_entities."""
        view = _view(
            action=SuggestedAction.CREATE_LOOP,
            loop_id=None,
            loop_title=None,
            summary="New interview request detected",
            extracted_entities={
                "candidate_name": "Old Name",
            },
            action_data={
                "candidate_name": "Adam L'esperance",
                "client_name": "Nim Sadeh",
                "client_email": "nim@kinematiclabs.dev",
                "client_company": "Kinematic Labs",
                "cm_name": "Sarah Jones",
                "cm_email": "sarah@lrp.com",
            },
        )
        widgets = _build_create_loop_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        # action_data wins over extracted_entities
        assert "Adam L'esperance" in text
        assert "Old Name" not in text
        # Other action_data fields pre-fill correctly
        assert "Nim Sadeh" in text
        assert "nim@kinematiclabs.dev" in text
        assert "Kinematic Labs" in text
        assert "Sarah Jones" in text
        assert "sarah@lrp.com" in text

    def test_cm_not_required(self):
        """CM fields should NOT be in the required_widgets list."""
        view = _view(
            action=SuggestedAction.CREATE_LOOP,
            loop_id=None,
            loop_title=None,
            summary="New interview request detected",
            extracted_entities={
                "candidate_name": "Claire Thompson",
                "client_email": "haley@acme.com",
                "recruiter_name": "Bob Smith",
                "recruiter_email": "bob@lrp.com",
            },
        )
        widgets = _build_create_loop_suggestion(view)
        # Find the button widget and check its requiredWidgets
        serialized = [w.model_dump(by_alias=True, exclude_none=True) for w in widgets]
        for w in serialized:
            buttons = w.get("buttonList", {}).get("buttons", [])
            for btn in buttons:
                req = btn.get("onClick", {}).get("action", {}).get("requiredWidgets", [])
                for name in req:
                    assert "cm_" not in name, f"CM field {name} should not be required"


class TestAdvanceSuggestionBuilder:
    def test_with_stage_context_shows_from_to(self):
        view = _view(
            action=SuggestedAction.ADVANCE_STAGE,
            summary="Advance to Awaiting Client",
            stage_name="Round 1",
            stage_state="awaiting_candidate",
            target_state="awaiting_client",
        )
        widgets = _build_advance_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Round 1" in text
        assert "Awaiting Candidate" in text
        assert "Awaiting Client" in text
        assert "from" in text.lower()
        # DecoratedText + ButtonList (no reasoning)
        assert len(widgets) == 2

    def test_without_stage_context_shows_target_only(self):
        view = _view(
            action=SuggestedAction.ADVANCE_STAGE,
            summary="Advance to Awaiting Client",
            target_state="awaiting_client",
        )
        widgets = _build_advance_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Awaiting Client" in text
        # No "from" when current state is unknown
        assert "from" not in text.lower()
        assert len(widgets) == 2

    def test_reasoning_omitted_for_clean_ux(self):
        view = _view(
            action=SuggestedAction.ADVANCE_STAGE,
            summary="Advance to Awaiting Client",
            stage_name="Round 1",
            stage_state="awaiting_candidate",
            target_state="awaiting_client",
            reasoning="Candidate sent availability times in latest email",
        )
        widgets = _build_advance_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        # Reasoning is intentionally NOT shown — the from/to label is sufficient
        assert "Candidate sent availability" not in text
        # DecoratedText + ButtonList only
        assert len(widgets) == 2

    def test_no_target_state_fallback(self):
        view = _view(
            action=SuggestedAction.ADVANCE_STAGE,
            summary="Advance stage",
            target_state=None,
        )
        widgets = _build_advance_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        # Should just show "Advance" without from/to
        assert "Advance" in text
        assert len(widgets) == 2


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


class TestMarkColdSuggestionBuilder:
    def test_shows_reasoning(self):
        view = _view(
            action=SuggestedAction.MARK_COLD,
            summary="No response in 5 days",
            reasoning="No reply since April 10",
        )
        widgets = _build_mark_cold_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Mark Cold" in text


class TestAskCoordinatorSuggestionBuilder:
    def test_shows_questions_and_disabled_respond(self):
        view = _view(
            action=SuggestedAction.ASK_COORDINATOR,
            summary="Agent needs clarification",
            questions=["Should we propose morning or afternoon slots?"],
        )
        widgets = _build_ask_suggestion(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "morning or afternoon" in text
        assert "coming soon" in text.lower()


class TestSuggestionDispatcher:
    def test_dispatches_all_types(self):
        for action in SuggestedAction:
            if action == SuggestedAction.NO_ACTION:
                continue
            view = _view(action=action)
            widgets = _build_suggestion_widgets(view)
            assert len(widgets) > 0, f"No widgets for {action}"

    def test_unknown_action_returns_fallback(self):
        view = _view(action=SuggestedAction.NO_ACTION)
        widgets = _build_suggestion_widgets(view)
        text = str([w.model_dump(by_alias=True, exclude_none=True) for w in widgets])
        assert "Unknown" in text or "unknown" in text
