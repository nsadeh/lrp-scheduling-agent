"""Card builders for the suggestion-centric overview UI.

Pure functions returning CardResponse models for the Gmail sidebar.
Reuses shared helpers from scheduling/cards.py.
"""

from __future__ import annotations

from api.addon.models import (
    Button,
    Card,
    CardResponse,
    OnClick,
    OpenLink,
    Section,
    TextInput,
    TextInputWidget,
    Widget,
)
from api.classifier.models import SuggestedAction
from api.overview.models import (  # noqa: TC001 — needed at runtime
    LoopSuggestionGroup,
    SuggestionView,
)
from api.scheduling.cards import (
    _action,
    _button,
    _buttons,
    _decorated,
    _divider,
    _text,
    _update_card,
    set_action_url,  # noqa: F401 — re-exported for routes
)

# ---------------------------------------------------------------------------
# Tab navigation
# ---------------------------------------------------------------------------


def _overview_header_buttons(base_url: str | None = None) -> Section | None:
    """Render header buttons (refresh only)."""
    buttons = []

    if base_url:
        refresh_btn = Button(
            text="\u21bb Refresh",
            on_click=OnClick(
                open_link=OpenLink(
                    url=f"{base_url}/addon/refresh",
                    open_as="OVERLAY",
                    on_close="RELOAD",
                )
            ),
        )
        buttons.append(refresh_btn)

    if not buttons:
        return None
    return Section(widgets=[_buttons(*buttons)])


# ---------------------------------------------------------------------------
# Per-action-type widget builders
# ---------------------------------------------------------------------------


def _dismiss_button(suggestion_id: str) -> Button:
    """Dismiss button shared across all suggestion types."""
    return Button(
        text="\u2715",
        on_click=_action("reject_suggestion", suggestion_id=suggestion_id),
    )


def _build_draft_suggestion(view: SuggestionView) -> list[Widget]:
    """DRAFT_EMAIL — inline editable draft with Send/Forward + Dismiss."""
    widgets: list[Widget] = []
    sug = view.suggestion
    draft = view.draft

    # Summary header
    widgets.append(_text(f"<b>\u2709 {sug.summary}</b>"))

    if draft:
        is_fwd = draft.is_forward

        # Recipients (read-only)
        widgets.append(_decorated(", ".join(draft.to_emails), "To"))
        if draft.cc_emails:
            widgets.append(_decorated(", ".join(draft.cc_emails), "CC"))
        widgets.append(_decorated(draft.subject, "Subject"))
        widgets.append(_divider())

        # Editable body — unique name per suggestion to avoid collisions.
        # For forwards the note is optional (coordinator may just forward
        # without commentary); for replies the message is always required.
        input_name = f"draft_body_{sug.id}"
        widgets.append(
            TextInputWidget(
                text_input=TextInput(
                    name=input_name,
                    label="Forward note" if is_fwd else "Message",
                    type="MULTIPLE_LINE",
                    value=draft.body,
                )
            )
        )

        # Dismiss (left) / Send or Forward (right)
        send_label = "Forward" if is_fwd else "Send"
        send_required = [] if is_fwd else [input_name]
        widgets.append(
            _buttons(
                _dismiss_button(sug.id),
                _button(
                    send_label,
                    "send_draft",
                    required_widgets=send_required,
                    draft_id=draft.id,
                    suggestion_id=sug.id,
                ),
            )
        )
    else:
        # Draft not yet generated — show refresh button so user can re-check
        widgets.append(_text("<i>Draft is being generated\u2026 tap Refresh to check.</i>"))
        widgets.append(
            _buttons(
                _button("\u21bb Refresh", "show_suggestions_tab"),
                _dismiss_button(sug.id),
            )
        )

    return widgets


def _build_create_loop_suggestion(view: SuggestionView) -> list[Widget]:
    """CREATE_LOOP — inline form with pre-filled TextInputs from extracted entities.

    Renders an editable form directly in the suggestion card so coordinators
    can review/edit the extracted data and create the loop with one click —
    no navigation to a separate form.
    """
    widgets: list[Widget] = []
    sug = view.suggestion
    entities = sug.extracted_entities
    action_data = sug.action_data or {}
    sid = sug.id  # suffix for unique input names

    # Read from action_data first, fall back to extracted_entities
    def _val(key: str, default: str = "") -> str:
        return action_data.get(key) or entities.get(key, default)

    widgets.append(_text("<b>+ New loop detected</b>"))

    # Inline form fields — pre-filled from classifier output
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"candidate_name_{sid}",
                label="Candidate Name",
                type="SINGLE_LINE",
                value=_val("candidate_name"),
            )
        )
    )
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"client_name_{sid}",
                label="Client Contact Name",
                type="SINGLE_LINE",
                value=_val("client_name"),
            )
        )
    )
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"client_email_{sid}",
                label="Client Email",
                type="SINGLE_LINE",
                value=_val("client_email"),
            )
        )
    )
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"client_company_{sid}",
                label="Client Company",
                type="SINGLE_LINE",
                value=_val("client_company"),
            )
        )
    )
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"recruiter_name_{sid}",
                label="Recruiter Name",
                type="SINGLE_LINE",
                value=_val("recruiter_name"),
            )
        )
    )
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"recruiter_email_{sid}",
                label="Recruiter Email",
                type="SINGLE_LINE",
                value=_val("recruiter_email"),
            )
        )
    )

    # Client Manager — optional
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"cm_name_{sid}",
                label="Client Manager Name (optional)",
                type="SINGLE_LINE",
                value=_val("cm_name"),
            )
        )
    )
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"cm_email_{sid}",
                label="Client Manager Email (optional)",
                type="SINGLE_LINE",
                value=_val("cm_email"),
            )
        )
    )

    # Create button — calls create_loop directly (no form navigation)
    required = [
        f"candidate_name_{sid}",
        f"client_email_{sid}",
        f"recruiter_name_{sid}",
        f"recruiter_email_{sid}",
    ]

    create_params: dict[str, str] = {
        "suggestion_id": sug.id,
    }
    if sug.gmail_thread_id:
        create_params["gmail_thread_id"] = sug.gmail_thread_id
    if sug.gmail_message_id:
        create_params["gmail_message_id"] = sug.gmail_message_id

    widgets.append(
        _buttons(
            _button(
                "Create Loop",
                "create_loop",
                required_widgets=required,
                **create_params,
            ),
            _dismiss_button(sug.id),
        )
    )

    return widgets


def _format_state_label(state: str) -> str:
    """Turn a StageState value like 'awaiting_client' into 'Awaiting Client'."""
    return state.replace("_", " ").title()


def _build_advance_suggestion(view: SuggestionView) -> list[Widget]:
    """ADVANCE_STAGE — concise "from → to" label with Accept/Dismiss.

    Reasoning is intentionally omitted from the card — the from/to label
    is self-explanatory for the coordinator, and classifier reasoning
    would clutter the one-click approve flow.
    """
    sug = view.suggestion

    # Build a descriptive label: "Advance <stage> from <current> to <target>"
    parts = ["Advance"]
    if view.stage_name:
        parts.append(view.stage_name)
    current_label = _format_state_label(view.stage_state) if view.stage_state else None
    target_label = _format_state_label(sug.target_state.value) if sug.target_state else None
    if current_label and target_label:
        parts.append(f"from {current_label} to {target_label}")
    elif target_label:
        parts.append(f"to {target_label}")
    label = " ".join(parts)

    return [
        _decorated(label, "\u2191 Advance"),
        _buttons(
            _button("Accept", "accept_suggestion", suggestion_id=sug.id),
            _dismiss_button(sug.id),
        ),
    ]


def _build_link_thread_suggestion(view: SuggestionView) -> list[Widget]:
    """LINK_THREAD — target loop + collapsible reasoning."""
    sug = view.suggestion

    # Link target display
    target_title = view.loop_title or sug.summary
    widgets: list[Widget] = [
        _decorated(target_title, "\U0001f517 Link to"),
    ]

    # Collapsible reasoning
    if sug.reasoning:
        widgets.append(_text(f"<i>{sug.reasoning}</i>"))

    widgets.append(
        _buttons(
            _button("Link", "accept_suggestion", suggestion_id=sug.id),
            _dismiss_button(sug.id),
        )
    )
    return widgets


def _build_mark_cold_suggestion(view: SuggestionView) -> list[Widget]:
    """MARK_COLD — reasoning + one-click."""
    sug = view.suggestion
    widgets: list[Widget] = [
        _decorated(sug.summary, "\u2744 Mark Cold"),
    ]

    if sug.reasoning:
        widgets.append(_text(f"<i>{sug.reasoning}</i>"))

    widgets.append(
        _buttons(
            _button("Mark Cold", "accept_suggestion", suggestion_id=sug.id),
            _dismiss_button(sug.id),
        )
    )
    return widgets


def _build_ask_suggestion(view: SuggestionView) -> list[Widget]:
    """ASK_COORDINATOR — question + text input + disabled respond."""
    sug = view.suggestion
    widgets: list[Widget] = [
        _text("<b>\u2753 Agent needs clarification</b>"),
    ]

    # Show question(s)
    for q in sug.questions:
        widgets.append(_text(f'"{q}"'))

    # Text input for response
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"coordinator_response_{sug.id}",
                label="Your response",
                type="MULTIPLE_LINE",
            )
        )
    )

    # Respond button (disabled — backend not implemented yet)
    widgets.append(
        _buttons(
            Button(
                text="Respond (coming soon)",
                on_click=_action("accept_suggestion", suggestion_id=sug.id),
                disabled=True,
            ),
            _dismiss_button(sug.id),
        )
    )
    return widgets


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_SUGGESTION_BUILDERS = {
    SuggestedAction.DRAFT_EMAIL: _build_draft_suggestion,
    SuggestedAction.CREATE_LOOP: _build_create_loop_suggestion,
    SuggestedAction.ADVANCE_STAGE: _build_advance_suggestion,
    SuggestedAction.LINK_THREAD: _build_link_thread_suggestion,
    SuggestedAction.MARK_COLD: _build_mark_cold_suggestion,
    SuggestedAction.ASK_COORDINATOR: _build_ask_suggestion,
}


def _build_suggestion_widgets(view: SuggestionView) -> list[Widget]:
    """Dispatch to the appropriate per-type builder."""
    builder = _SUGGESTION_BUILDERS.get(view.suggestion.action)
    if builder:
        return builder(view)
    # Fallback for unknown action types
    return [_text(f"<i>Unknown suggestion: {view.suggestion.summary}</i>")]


# ---------------------------------------------------------------------------
# Overview card
# ---------------------------------------------------------------------------


def build_overview(
    groups: list[LoopSuggestionGroup],
    base_url: str | None = None,
) -> CardResponse:
    """Build the main suggestion-centric overview card.

    Each loop group becomes a Section. Standalone suggestions (no loop)
    are rendered in a headerless section at the top.
    """
    sections: list[Section] = []
    header_section = _overview_header_buttons(base_url)
    if header_section:
        sections.append(header_section)

    if not groups:
        # Empty state
        sections.append(
            Section(
                widgets=[
                    _text("All caught up \u2014 no actions needed."),
                ]
            )
        )
        return _update_card(Card(sections=sections))

    for group in groups:
        # Build all widgets for suggestions in this group
        group_widgets: list[Widget] = []
        for i, view in enumerate(group.suggestions):
            if i > 0:
                group_widgets.append(_divider())
            group_widgets.extend(_build_suggestion_widgets(view))

        # Section header: loop title or "Unassigned" for loop-less
        header = None
        if group.loop_id and group.loop_title:
            header = group.loop_title
        elif group.loop_id:
            # Loop exists but no title — use candidate/company if available
            parts = [p for p in [group.candidate_name, group.client_company] if p]
            header = ", ".join(parts) if parts else f"Loop {group.loop_id[:8]}"

        # Add "Open in Gmail" link if the loop has threads (first thread)
        # This is added as a small button in the section
        sections.append(Section(header=header, widgets=group_widgets))

    return _update_card(Card(sections=sections))
