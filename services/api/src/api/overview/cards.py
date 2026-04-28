"""Card builders for the suggestion-centric overview UI.

Pure functions returning CardResponse models for the Gmail sidebar.
Reuses shared helpers from scheduling/cards.py.

CREATE_LOOP / ADVANCE_STAGE / LINK_THREAD are auto-resolved by
classifier/resolvers.py for new suggestions (the resolver marks them
AUTO_APPLIED and the SQL filter `status='pending'` hides them). The
original card builders for these actions are kept so:
  1. Pre-deploy PENDING rows still render with the old UI — coordinators
     can clear the backlog rather than have it disappear.
  2. If a resolver fails (Sentry-and-drop), the suggestion stays PENDING
     and the coordinator can finish manually.
"""

from __future__ import annotations

from api.addon.contact_inputs import build_client_inputs, build_recruiter_inputs
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
from api.classifier.resolvers import DEFAULT_CANDIDATE_NAME
from api.overview.models import (  # noqa: TC001 - needed at runtime
    LoopSuggestionGroup,
    SuggestionView,
)
from api.scheduling.cards import (
    _action,
    _button,
    _buttons,
    _decorated,
    _directory_autocomplete_action,
    _divider,
    _text,
    _update_card,
    directory_search_url,
    get_action_url,
    set_action_url,  # noqa: F401 - re-exported for routes
)
from api.scheduling.models import StageState

# Stage states where the draft is sent to the recruiter (vs. client).
# Mirrors `resolve_recipients` in drafts/service.py - the single source
# of truth for routing - but we need it inline at render time to decide
# which JIT inputs to show.
_RECRUITER_STAGES = {StageState.NEW.value}


# ---------------------------------------------------------------------------
# Tab navigation
# ---------------------------------------------------------------------------


def _overview_header_buttons(base_url: str | None = None) -> Section | None:
    """Render header buttons (refresh only)."""
    buttons = []

    if base_url:
        refresh_btn = Button(
            text="↻ Refresh",
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
        text="✕",
        on_click=_action("reject_suggestion", suggestion_id=suggestion_id),
    )


def _format_known_actors(view: SuggestionView, *, exclude: str) -> str:
    """Render a small-print line of actor emails the loop already has.

    Used as a context hint under the JIT input — when we ask for the
    recruiter, show client/CM emails we know; when we ask for the client,
    show recruiter/CM. ``exclude`` skips the role we're asking for.
    """
    parts: list[str] = []
    if exclude != "recruiter" and view.recruiter_email:
        label = view.recruiter_name or "Recruiter"
        parts.append(f"Recruiter: {label} &lt;{view.recruiter_email}&gt;")
    if exclude != "client_contact" and view.client_contact_email:
        label = view.client_contact_name or "Client"
        parts.append(f"Client: {label} &lt;{view.client_contact_email}&gt;")
    if view.client_manager_email:
        label = view.client_manager_name or "CM"
        parts.append(f"CM: {label} &lt;{view.client_manager_email}&gt;")
    if not parts:
        return ""
    return "Known on this loop — " + "; ".join(parts)


def _missing_recipient_role(view: SuggestionView) -> tuple[bool, bool]:
    """Return (needs_recruiter, needs_client) for a draft view.

    A draft has a missing recipient when its `to_emails` is empty - the
    loop's relevant contact is null. The role is determined by stage state
    (same logic as `resolve_recipients` in drafts/service.py). Mutually
    exclusive: a draft only ever needs one role at a time.
    """
    draft = view.draft
    if draft is None or draft.to_emails:
        return (False, False)
    if view.stage_state in _RECRUITER_STAGES:
        return (True, False)
    return (False, True)


def _build_draft_suggestion(view: SuggestionView) -> list[Widget]:
    """DRAFT_EMAIL - inline editable draft with Send/Forward + Dismiss.

    When the draft has no recipient (the loop's recruiter or client_contact
    is null because the loop was auto-created with incomplete info), this
    card collects the missing contact inline using the shared autocomplete
    helpers and disables the Send button until an email is supplied.
    """
    widgets: list[Widget] = []
    sug = view.suggestion
    draft = view.draft

    # Summary header
    widgets.append(_text(f"<b>✉ {sug.summary}</b>"))

    if draft:
        is_fwd = draft.is_forward
        needs_recruiter, needs_client = _missing_recipient_role(view)

        # Recipients: either show the resolved To/CC, OR collect inline.
        if draft.to_emails:
            widgets.append(_decorated(", ".join(draft.to_emails), "To"))
            if draft.cc_emails:
                widgets.append(_decorated(", ".join(draft.cc_emails), "CC"))
        elif needs_recruiter:
            widgets.append(_text("<i>Add the recruiter so this draft can be sent.</i>"))
            widgets.extend(
                build_recruiter_inputs(
                    action_url=get_action_url(),
                    directory_search_url=directory_search_url(),
                    name_field=f"jit_recruiter_name_{sug.id}",
                    email_field=f"jit_recruiter_email_{sug.id}",
                    on_change_extra_params={
                        "suggestion_id": sug.id,
                        "draft_id": draft.id,
                    },
                )
            )
            known = _format_known_actors(view, exclude="recruiter")
            if known:
                widgets.append(_text(f'<font color="#888888"><small>{known}</small></font>'))
        elif needs_client:
            widgets.append(_text("<i>Add the client contact so this draft can be sent.</i>"))
            widgets.extend(
                build_client_inputs(
                    name_field=f"jit_client_name_{sug.id}",
                    email_field=f"jit_client_email_{sug.id}",
                    company_field=f"jit_client_company_{sug.id}",
                )
            )
            known = _format_known_actors(view, exclude="client_contact")
            if known:
                widgets.append(_text(f'<font color="#888888"><small>{known}</small></font>'))

        widgets.append(_decorated(draft.subject, "Subject"))
        widgets.append(_divider())

        # Editable body - unique name per suggestion to avoid collisions.
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

        # Send/Forward button - disabled when there's no recipient yet.
        # The recruiter onChangeAction triggers a card refresh, so once
        # the coordinator picks an email the button re-renders enabled.
        send_label = "Forward" if is_fwd else "Send"
        send_required = [] if is_fwd else [input_name]
        send_disabled = not draft.to_emails and (needs_recruiter or needs_client)
        send_button = Button(
            text=send_label,
            on_click=_action(
                "send_draft",
                required_widgets=send_required,
                draft_id=draft.id,
                suggestion_id=sug.id,
            ),
            disabled=send_disabled,
        )
        widgets.append(_buttons(_dismiss_button(sug.id), send_button))
    else:
        # Draft not yet generated - show refresh button so user can re-check
        widgets.append(_text("<i>Draft is being generated… tap Refresh to check.</i>"))
        widgets.append(
            _buttons(
                _button("↻ Refresh", "show_suggestions_tab"),
                _dismiss_button(sug.id),
            )
        )

    return widgets


def _build_mark_cold_suggestion(view: SuggestionView) -> list[Widget]:
    """MARK_COLD - reasoning + one-click."""
    sug = view.suggestion
    widgets: list[Widget] = [
        _decorated(sug.summary, "❄ Mark Cold"),
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
    """ASK_COORDINATOR - question + text input + disabled respond."""
    sug = view.suggestion
    widgets: list[Widget] = [
        _text("<b>❓ Agent needs clarification</b>"),
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

    # Respond button (disabled - backend not implemented yet)
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


def _build_create_loop_suggestion(view: SuggestionView) -> list[Widget]:
    """CREATE_LOOP - inline form with pre-filled TextInputs.

    New CREATE_LOOP suggestions are auto-resolved (status=AUTO_APPLIED) and
    never reach this builder. It runs only for: (1) PENDING rows that
    pre-date the auto-resolver deploy, so the backlog is finishable; and
    (2) post-deploy rows whose resolver raised and was Sentry-and-dropped.
    Both cases want the original click-to-create form.
    """
    widgets: list[Widget] = []
    sug = view.suggestion
    entities = sug.extracted_entities
    action_data = sug.action_data or {}
    sid = sug.id

    def _val(key: str, default: str = "") -> str:
        return action_data.get(key) or entities.get(key, default)

    widgets.append(_text("<b>+ New loop detected</b>"))

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
    recruiter_autocomplete = _directory_autocomplete_action()
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name=f"recruiter_name_{sid}",
                label="Recruiter Name",
                type="SINGLE_LINE",
                value=_val("recruiter_name"),
                hint_text="Type to search your Workspace directory",
                auto_complete_action=recruiter_autocomplete,
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
                auto_complete_action=recruiter_autocomplete,
            )
        )
    )
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

    create_params: dict[str, str] = {"suggestion_id": sug.id}
    if sug.gmail_thread_id:
        create_params["gmail_thread_id"] = sug.gmail_thread_id
    if sug.gmail_message_id:
        create_params["gmail_message_id"] = sug.gmail_message_id

    widgets.append(
        _buttons(
            _button(
                "Create Loop",
                "create_loop",
                required_widgets=[f"candidate_name_{sid}"],
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
    """ADVANCE_STAGE - concise "from -> to" label with Accept/Dismiss.

    Same fallback story as _build_create_loop_suggestion: only renders for
    pre-deploy backlog or resolver-failure cases. New ADVANCE_STAGE
    suggestions are AUTO_APPLIED and filtered out.
    """
    sug = view.suggestion

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
        _decorated(label, "↑ Advance"),
        _buttons(
            _button("Accept", "accept_suggestion", suggestion_id=sug.id),
            _dismiss_button(sug.id),
        ),
    ]


def _build_link_thread_suggestion(view: SuggestionView) -> list[Widget]:
    """LINK_THREAD - target loop + collapsible reasoning.

    Backlog/failure fallback only — new LINK_THREAD suggestions are
    AUTO_APPLIED.
    """
    sug = view.suggestion
    target_title = view.loop_title or sug.summary
    widgets: list[Widget] = [
        _decorated(target_title, "🔗 Link to"),
    ]
    if sug.reasoning:
        widgets.append(_text(f"<i>{sug.reasoning}</i>"))
    widgets.append(
        _buttons(
            _button("Link", "accept_suggestion", suggestion_id=sug.id),
            _dismiss_button(sug.id),
        )
    )
    return widgets


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# All builders are present. CREATE_LOOP / ADVANCE_STAGE / LINK_THREAD are
# auto-resolved for new suggestions, but their builders stay in the
# dispatcher so the pre-deploy backlog and resolver-failure cases still
# render usefully.
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
    return [_text(f"<i>Unknown suggestion: {view.suggestion.summary}</i>")]


# ---------------------------------------------------------------------------
# Candidate rename affordance
# ---------------------------------------------------------------------------


def _build_candidate_rename(group: LoopSuggestionGroup) -> list[Widget]:
    """Inline rename input shown when the loop has a placeholder candidate.

    Only rendered when `candidate_name == "Unknown Candidate"` - i.e. the
    classifier auto-resolved CREATE_LOOP without a candidate name. The
    coordinator can fix it from the same card without leaving the sidebar.
    """
    if not group.loop_id or group.candidate_name != DEFAULT_CANDIDATE_NAME:
        return []
    field_name = f"candidate_name_{group.loop_id}"
    return [
        _text("<i>Candidate name not detected. Set it here:</i>"),
        TextInputWidget(
            text_input=TextInput(
                name=field_name,
                label="Candidate Name",
                type="SINGLE_LINE",
            )
        ),
        _buttons(
            _button(
                "Save name",
                "update_candidate_name",
                required_widgets=[field_name],
                loop_id=group.loop_id,
            ),
        ),
    ]


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
                    _text("All caught up — no actions needed."),
                ]
            )
        )
        return _update_card(Card(sections=sections))

    for group in groups:
        # Build all widgets for suggestions in this group
        group_widgets: list[Widget] = []

        # Candidate rename (only for placeholder names) appears at the top
        # of the section, before any suggestion widgets.
        rename_widgets = _build_candidate_rename(group)
        if rename_widgets:
            group_widgets.extend(rename_widgets)
            group_widgets.append(_divider())

        for i, view in enumerate(group.suggestions):
            if i > 0:
                group_widgets.append(_divider())
            group_widgets.extend(_build_suggestion_widgets(view))

        # Section header: loop title or "Unassigned" for loop-less
        header = None
        if group.loop_id and group.loop_title:
            header = group.loop_title
        elif group.loop_id:
            # Loop exists but no title - use candidate/company if available
            parts = [p for p in [group.candidate_name, group.client_company] if p]
            header = ", ".join(parts) if parts else f"Loop {group.loop_id[:8]}"

        # Add "Open in Gmail" link if the loop has threads (first thread)
        # This is added as a small button in the section
        sections.append(Section(header=header, widgets=group_widgets))

    return _update_card(Card(sections=sections))
