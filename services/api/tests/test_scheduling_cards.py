"""Tests for scheduling card builder functions."""

from api.scheduling.cards import (
    build_contextual_unlinked,
    build_create_loop_form,
    build_loop_pending_card,
    set_action_url,
)

# Set a test action URL so card builders can generate callback URLs
set_action_url("https://test.example.com/addon/action")


def _card_json(card_response):
    return card_response.model_dump(by_alias=True, exclude_none=True)


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

    def test_cm_section_is_collapsible(self):
        data = _card_json(build_create_loop_form())
        card = data["action"]["navigations"][0]["updateCard"]
        cm_section = next(
            s for s in card["sections"] if s.get("header", "").startswith("Client Manager")
        )
        assert cm_section["collapsible"] is True
        assert cm_section["uncollapsibleWidgetsCount"] == 1

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


class TestLoopPendingCard:
    def test_has_refresh_button_with_reload(self):
        data = _card_json(build_loop_pending_card("thread_xyz"))
        card = data["action"]["navigations"][0]["updateCard"]
        buttons = [
            b
            for s in card["sections"]
            for w in s["widgets"]
            if "buttonList" in w
            for b in w["buttonList"]["buttons"]
        ]
        assert any("Refresh" in b["text"] for b in buttons)
        refresh = next(b for b in buttons if "Refresh" in b["text"])
        link = refresh["onClick"]["openLink"]
        # The Refresh button must use OVERLAY + RELOAD so that closing the
        # overlay re-fires the contextual on-message trigger and the
        # newly-generated suggestions surface.
        assert link["url"].endswith("/addon/refresh")
        assert link["openAs"] == "OVERLAY"
        assert link["onClose"] == "RELOAD"

    def test_does_not_render_other_loops_suggestions(self):
        # Sanity check: this card is intentionally a placeholder and must
        # not include a suggestions list. Widening to the global overview
        # is exactly the bug this card replaces.
        data = _card_json(build_loop_pending_card("thread_xyz"))
        card = data["action"]["navigations"][0]["updateCard"]
        assert card["header"]["title"] == "Loop created"
        # Single section, two widgets: text paragraph + button list.
        assert len(card["sections"]) == 1
        widget_kinds = {next(iter(w.keys())) for w in card["sections"][0]["widgets"]}
        assert widget_kinds == {"textParagraph", "buttonList"}
