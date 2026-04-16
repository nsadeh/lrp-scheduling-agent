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


def _overview_tab_buttons(active_tab: str, base_url: str | None = None) -> Section:
    """Render tab toggle buttons with optional refresh."""
    suggestions_btn = Button(
        text="Suggestions",
        on_click=_action("show_suggestions_tab"),
        disabled=active_tab == "suggestions",
    )
    board_btn = Button(
        text="Status Board",
        on_click=_action("show_status_tab"),
        disabled=active_tab == "status",
    )
    buttons = [suggestions_btn, board_btn]

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
    """DRAFT_EMAIL — inline editable draft with Send/Edit/Dismiss."""
    widgets: list[Widget] = []
    sug = view.suggestion
    draft = view.draft

    # Summary header
    widgets.append(_text(f"<b>\u2709 {sug.summary}</b>"))

    if draft:
        # Recipients (read-only)
        widgets.append(_decorated(", ".join(draft.to_emails), "To"))
        if draft.cc_emails:
            widgets.append(_decorated(", ".join(draft.cc_emails), "CC"))
        widgets.append(_decorated(draft.subject, "Subject"))
        widgets.append(_divider())

        # Editable body — unique name per suggestion to avoid collisions
        input_name = f"draft_body_{sug.id}"
        widgets.append(
            TextInputWidget(
                text_input=TextInput(
                    name=input_name,
                    label="Message",
                    type="MULTIPLE_LINE",
                    value=draft.body,
                )
            )
        )

        # Action buttons
        widgets.append(
            _buttons(
                _button(
                    "Send",
                    "send_draft",
                    required_widgets=[input_name],
                    draft_id=draft.id,
                    suggestion_id=sug.id,
                ),
                _button("Edit & Send", "edit_draft", draft_id=draft.id),
                _dismiss_button(sug.id),
            )
        )
    else:
        # Draft not yet generated or missing
        widgets.append(_text("<i>Draft is being generated...</i>"))
        widgets.append(_buttons(_dismiss_button(sug.id)))

    return widgets


def _build_create_loop_suggestion(view: SuggestionView) -> list[Widget]:
    """CREATE_LOOP — extracted entities + create button."""
    widgets: list[Widget] = []
    sug = view.suggestion
    entities = sug.extracted_entities

    widgets.append(_text("<b>+ New loop detected</b>"))

    # Main entity fields
    candidate = entities.get("candidate_name", "Unknown")
    client_name = entities.get("client_name", "")
    client_email = entities.get("client_email", "")
    client_company = entities.get("client_company", "")
    recruiter_name = entities.get("recruiter_name", "")
    recruiter_email = entities.get("recruiter_email", "")

    widgets.append(_decorated(candidate, "Candidate"))
    client_display = f"{client_name} @ {client_company}" if client_company else client_name
    if client_email:
        client_display += f" ({client_email})"
    widgets.append(_decorated(client_display, "Client"))
    recruiter_display = recruiter_name
    if recruiter_email:
        recruiter_display += f" ({recruiter_email})"
    widgets.append(_decorated(recruiter_display, "Recruiter"))

    # Build create button params — pass extracted entities to pre-fill the form
    create_params: dict[str, str] = {"suggestion_id": sug.id}
    if sug.gmail_thread_id:
        create_params["gmail_thread_id"] = sug.gmail_thread_id
    # Pre-fill params for the create form
    for key in (
        "candidate_name",
        "client_name",
        "client_email",
        "client_company",
        "recruiter_name",
        "recruiter_email",
    ):
        val = entities.get(key)
        if val:
            create_params[f"prefill_{key}"] = val

    # Optional: client manager in collapsible section
    cm_name = entities.get("client_manager_name")
    cm_email = entities.get("client_manager_email")
    if cm_name or cm_email:
        cm_display = cm_name or ""
        if cm_email:
            cm_display += f" ({cm_email})"
            create_params["prefill_cm_name"] = cm_name or ""
            create_params["prefill_cm_email"] = cm_email

    widgets.append(
        _buttons(
            _button("Create Loop", "show_create_form", **create_params),
            _dismiss_button(sug.id),
        )
    )

    return widgets


def _build_advance_suggestion(view: SuggestionView) -> list[Widget]:
    """ADVANCE_STAGE — cardless one-liner with Accept button."""
    sug = view.suggestion
    return [
        _decorated(sug.summary, "\u2191 Advance"),
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
    sections: list[Section] = [_overview_tab_buttons("suggestions", base_url)]

    if not groups:
        # Empty state
        sections.append(
            Section(
                widgets=[
                    _text("All caught up \u2014 no actions needed."),
                    _buttons(_button("Status Board", "show_status_tab")),
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
