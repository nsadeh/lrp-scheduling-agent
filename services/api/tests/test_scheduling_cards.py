"""Tests for scheduling card builder functions."""

from datetime import UTC, datetime

from api.scheduling.cards import (
    _initials,
    build_compose_email,
    build_contextual_unlinked,
    build_create_loop_form,
    build_drafts_tab,
    build_loop_detail,
    build_status_board,
    set_action_url,
)
from api.scheduling.models import (
    Candidate,
    ClientContact,
    Contact,
    Coordinator,
    Loop,
    LoopSummary,
    Stage,
    StageState,
    StatusBoard,
)

NOW = datetime.now(UTC)

# Set a test action URL so card builders can generate callback URLs
set_action_url("https://test.example.com/addon/action")


def _make_loop(
    stages: list[Stage] | None = None,
    client_manager: Contact | None = "default",
) -> Loop:
    cm = (
        Contact(
            id="con_cm",
            name="Sarah Kim",
            email="sarah@lrp.com",
            role="client_manager",
            created_at=NOW,
        )
        if client_manager == "default"
        else client_manager
    )
    return Loop(
        id="lop_test",
        coordinator_id="crd_test",
        client_contact_id="cli_test",
        recruiter_id="con_rec",
        client_manager_id=cm.id if cm else None,
        candidate_id="can_test",
        title="Smith, Acme Capital",
        created_at=NOW,
        updated_at=NOW,
        coordinator=Coordinator(id="crd_test", name="Alice", email="alice@lrp.com", created_at=NOW),
        client_contact=ClientContact(
            id="cli_test",
            name="Jane Doe",
            email="jane@acme.com",
            company="Acme Capital",
            created_at=NOW,
        ),
        recruiter=Contact(
            id="con_rec", name="Bob Lee", email="bob@recruit.com", role="recruiter", created_at=NOW
        ),
        client_manager=cm,
        candidate=Candidate(id="can_test", name="John Smith", created_at=NOW),
        stages=stages
        or [
            Stage(
                id="stg_1",
                loop_id="lop_test",
                name="Round 1",
                state=StageState.NEW,
                ordinal=0,
                created_at=NOW,
                updated_at=NOW,
            ),
        ],
    )


def _card_json(card_response):
    return card_response.model_dump(by_alias=True, exclude_none=True)


class TestInitials:
    def test_two_words(self):
        assert _initials("Sarah Kim") == "SK"

    def test_single_word(self):
        assert _initials("Bob") == "B"

    def test_three_words(self):
        assert _initials("Mary Jane Watson") == "MJW"


def _make_summary(**overrides) -> LoopSummary:
    defaults = dict(
        loop_id="lop_1",
        title="Smith, Acme",
        candidate_name="Smith",
        client_company="Acme",
        most_urgent_stage_id="stg_1",
        most_urgent_stage_name="Round 1",
        most_urgent_next_action="Email recruiter for availability",
        most_urgent_state=StageState.NEW,
    )
    defaults.update(overrides)
    return LoopSummary(**defaults)


class TestDraftsTab:
    def test_empty_board_shows_get_started_message(self):
        board = StatusBoard()
        data = _card_json(build_drafts_tab(board))
        card = data["action"]["navigations"][0]["updateCard"]
        text = str(card)
        assert "create one" in text.lower()

    def test_empty_drafts_with_completed_loops_shows_hint(self):
        board = StatusBoard(complete=[_make_summary(most_urgent_state=StageState.COMPLETE)])
        data = _card_json(build_drafts_tab(board))
        card = data["action"]["navigations"][0]["updateCard"]
        text = str(card)
        assert "status board" in text.lower()

    def test_has_tab_buttons(self):
        board = StatusBoard()
        data = _card_json(build_drafts_tab(board))
        card = data["action"]["navigations"][0]["updateCard"]
        # First section should have tab buttons
        first_section = card["sections"][0]
        button_widgets = [w for w in first_section["widgets"] if "buttonList" in w]
        assert len(button_widgets) == 1
        btn_texts = [b["text"] for b in button_widgets[0]["buttonList"]["buttons"]]
        assert "Drafts" in btn_texts
        assert "Status Board" in btn_texts

    def test_drafts_tab_is_active(self):
        board = StatusBoard()
        data = _card_json(build_drafts_tab(board))
        card = data["action"]["navigations"][0]["updateCard"]
        first_section = card["sections"][0]
        buttons = first_section["widgets"][0]["buttonList"]["buttons"]
        drafts_btn = next(b for b in buttons if b["text"] == "Drafts")
        assert drafts_btn["disabled"] is True

    def test_action_needed_shows_inline_buttons(self):
        board = StatusBoard(action_needed=[_make_summary()])
        data = _card_json(build_drafts_tab(board))
        card = data["action"]["navigations"][0]["updateCard"]
        # Second section (after tabs) should have action buttons
        action_section = card["sections"][1]
        button_widgets = [w for w in action_section["widgets"] if "buttonList" in w]
        all_texts = [b["text"] for bw in button_widgets for b in bw["buttonList"]["buttons"]]
        assert "Forward to Recruiter" in all_texts
        assert "Go Cold" in all_texts

    def test_has_new_loop_button(self):
        board = StatusBoard()
        data = _card_json(build_drafts_tab(board))
        card = data["action"]["navigations"][0]["updateCard"]
        all_buttons = [
            b["text"]
            for s in card["sections"]
            for w in s["widgets"]
            if "buttonList" in w
            for b in w["buttonList"]["buttons"]
        ]
        assert "+ New Loop" in all_buttons


class TestStatusBoard:
    def test_has_tab_buttons(self):
        board = StatusBoard()
        data = _card_json(build_status_board(board))
        card = data["action"]["navigations"][0]["updateCard"]
        first_section = card["sections"][0]
        button_widgets = [w for w in first_section["widgets"] if "buttonList" in w]
        btn_texts = [b["text"] for b in button_widgets[0]["buttonList"]["buttons"]]
        assert "Drafts" in btn_texts
        assert "Status Board" in btn_texts

    def test_status_tab_is_active(self):
        board = StatusBoard()
        data = _card_json(build_status_board(board))
        card = data["action"]["navigations"][0]["updateCard"]
        first_section = card["sections"][0]
        buttons = first_section["widgets"][0]["buttonList"]["buttons"]
        status_btn = next(b for b in buttons if b["text"] == "Status Board")
        assert status_btn["disabled"] is True

    def test_groups_by_state_with_labels(self):
        board = StatusBoard(
            action_needed=[_make_summary(most_urgent_state=StageState.NEW)],
            waiting=[
                _make_summary(
                    loop_id="lop_2",
                    title="Jones, Beta",
                    most_urgent_state=StageState.AWAITING_CANDIDATE,
                )
            ],
        )
        data = _card_json(build_status_board(board))
        card = data["action"]["navigations"][0]["updateCard"]
        headers = [s.get("header", "") for s in card["sections"]]
        assert any("Action Needed" in h for h in headers)
        assert any("Waiting on Recruiter" in h for h in headers)

    def test_summary_has_colored_badge(self):
        board = StatusBoard(action_needed=[_make_summary(most_urgent_state=StageState.NEW)])
        data = _card_json(build_status_board(board))
        card = data["action"]["navigations"][0]["updateCard"]
        # Find the section with loop summary (not the tab section)
        summary_sections = [s for s in card["sections"] if s.get("header", "").startswith("Action")]
        assert len(summary_sections) == 1
        widget = summary_sections[0]["widgets"][0]
        top_label = widget["decoratedText"]["topLabel"]
        # Should contain colored dot HTML
        assert "●" in top_label
        assert "font color" in top_label


class TestContextualUnlinked:
    def test_shows_create_button(self):
        data = _card_json(build_contextual_unlinked("thread_123"))
        card = data["action"]["navigations"][0]["updateCard"]
        widgets = card["sections"][0]["widgets"]
        # Should have text and a button
        assert any("textParagraph" in w for w in widgets)
        assert any("buttonList" in w for w in widgets)

    def test_no_duplicate_header(self):
        data = _card_json(build_contextual_unlinked("thread_123"))
        card = data["action"]["navigations"][0]["updateCard"]
        assert "header" not in card


class TestLoopDetail:
    def test_no_actors_section(self):
        loop = _make_loop()
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        headers = [s.get("header", "") for s in card["sections"]]
        assert "Actors" not in headers

    def test_has_edit_loop_button(self):
        loop = _make_loop()
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        last_section = card["sections"][-1]
        button_texts = [
            b["text"]
            for w in last_section["widgets"]
            if "buttonList" in w
            for b in w["buttonList"]["buttons"]
        ]
        assert "Edit Loop" in button_texts

    def test_header_format_with_cm(self):
        loop = _make_loop()
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        title = card["header"]["title"]
        # Should be "SK/BL, Round 1, John Smith, Acme Capital"
        assert title == "SK/BL, Round 1, John Smith, Acme Capital"

    def test_header_format_without_cm(self):
        loop = _make_loop(client_manager=None)
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        title = card["header"]["title"]
        assert title == "BL, Round 1, John Smith, Acme Capital"

    def test_header_no_subtitle(self):
        loop = _make_loop()
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        assert "subtitle" not in card["header"]

    def test_shows_stage_with_state(self):
        loop = _make_loop()
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        # First section is the stage
        stage_section = card["sections"][0]
        assert "Round 1" in stage_section["header"]
        assert "New" in stage_section["header"]

    def test_new_stage_shows_forward_button(self):
        loop = _make_loop()
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        stage_section = card["sections"][0]
        button_widgets = [w for w in stage_section["widgets"] if "buttonList" in w]
        all_button_texts = [b["text"] for bw in button_widgets for b in bw["buttonList"]["buttons"]]
        assert "Forward to Recruiter" in all_button_texts
        assert "Send Email to Recruiter" not in all_button_texts

    def test_awaiting_candidate_shows_inline_textarea(self):
        stages = [
            Stage(
                id="stg_1",
                loop_id="lop_test",
                name="Round 1",
                state=StageState.AWAITING_CANDIDATE,
                ordinal=0,
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
        loop = _make_loop(stages=stages)
        data = _card_json(build_loop_detail(loop))
        card = data["action"]["navigations"][0]["updateCard"]
        stage_section = card["sections"][0]
        # Should have an inline text input for email body
        text_inputs = [w for w in stage_section["widgets"] if "textInput" in w]
        assert len(text_inputs) == 1
        assert text_inputs[0]["textInput"]["name"] == "email_body"
        # And a send button
        button_widgets = [w for w in stage_section["widgets"] if "buttonList" in w]
        all_button_texts = [b["text"] for bw in button_widgets for b in bw["buttonList"]["buttons"]]
        assert "Send Availability to Client" in all_button_texts


class TestCreateLoopForm:
    def test_has_multiple_sections(self):
        data = _card_json(build_create_loop_form())
        card = data["action"]["navigations"][0]["updateCard"]
        # Should have sections: Candidate, Client Contact, Recruiter, CM, Stage, Buttons
        assert len(card["sections"]) >= 5

    def test_has_required_fields(self):
        data = _card_json(build_create_loop_form())
        card = data["action"]["navigations"][0]["updateCard"]
        # Collect all text input names across all sections
        input_names = [
            w["textInput"]["name"]
            for s in card["sections"]
            for w in s["widgets"]
            if "textInput" in w
        ]
        assert "candidate_name" in input_names
        assert "client_name" in input_names
        assert "recruiter_name" in input_names
        assert "first_stage_name" in input_names

    def test_cm_section_is_collapsible(self):
        data = _card_json(build_create_loop_form())
        card = data["action"]["navigations"][0]["updateCard"]
        cm_section = next(
            s for s in card["sections"] if s.get("header", "").startswith("Client Manager")
        )
        assert cm_section["collapsible"] is True
        assert cm_section["uncollapsibleWidgetsCount"] == 0

    def test_create_button_has_required_widgets(self):
        data = _card_json(build_create_loop_form())
        card = data["action"]["navigations"][0]["updateCard"]
        # Find the button section (last one)
        button_section = card["sections"][-1]
        buttons = button_section["widgets"][0]["buttonList"]["buttons"]
        create_btn = next(b for b in buttons if b["text"] == "Create Loop")
        required = create_btn["onClick"]["action"]["requiredWidgets"]
        assert "candidate_name" in required
        assert "client_email" in required
        assert "recruiter_name" in required

    def test_has_create_button(self):
        data = _card_json(build_create_loop_form())
        card = data["action"]["navigations"][0]["updateCard"]
        all_buttons = [
            b["text"]
            for s in card["sections"]
            for w in s["widgets"]
            if "buttonList" in w
            for b in w["buttonList"]["buttons"]
        ]
        assert "Create Loop" in all_buttons


class TestComposeEmail:
    def test_shows_recipient_and_subject(self):
        loop = _make_loop()
        stage = loop.stages[0]
        data = _card_json(build_compose_email(loop, stage, "bob@recruit.com", "Re: Smith"))
        card = data["action"]["navigations"][0]["updateCard"]
        widgets = card["sections"][0]["widgets"]
        # Should show To and Subject as decorated text
        decorated = [w for w in widgets if "decoratedText" in w]
        texts = [d["decoratedText"]["text"] for d in decorated]
        assert "bob@recruit.com" in texts
        assert "Re: Smith" in texts

    def test_has_send_button(self):
        loop = _make_loop()
        stage = loop.stages[0]
        data = _card_json(build_compose_email(loop, stage, "bob@recruit.com", "Re: Smith"))
        card = data["action"]["navigations"][0]["updateCard"]
        widgets = card["sections"][0]["widgets"]
        button_widgets = [w for w in widgets if "buttonList" in w]
        all_texts = [b["text"] for bw in button_widgets for b in bw["buttonList"]["buttons"]]
        assert "Send" in all_texts
