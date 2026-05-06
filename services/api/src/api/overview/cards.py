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

    Used as a context hint under the JIT input. Shows the client contact
    and the client manager (when present) — never the recruiter, since
    showing the recruiter when we're asking for them is redundant, and
    showing them when asking for the client clutters the card.
    ``exclude`` skips the role we're currently asking for.
    """
    parts: list[str] = []
    if exclude != "client_contact" and view.client_contact_email:
        label = view.client_contact_name or "Client"
        parts.append(f"Client: {label} &lt;{view.client_contact_email}&gt;")
    if view.client_manager_email:
        label = view.client_manager_name or "CM"
        parts.append(f"CM: {label} &lt;{view.client_manager_email}&gt;")
    return " · ".join(parts)


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
    if view.loop_state in _RECRUITER_STAGES:
        return (True, False)
    return (False, True)


def _build_draft_suggestion(view: SuggestionView) -> list[Widget]:
    """DRAFT_EMAIL - inline editable draft with Send/Forward + Dismiss.

    When the loop is missing a contact this card needs (recruiter for
    NEW-stage drafts, client for later stages, CM whenever it's null),
    the card collects it inline. Picks are staged on
    ``draft.pending_jit_data`` rather than committed to the loop, so
    misclicks can be undone with the small "x" button before Send.
    Contacts are only created and attached to the loop at Send time.
    """
    widgets: list[Widget] = []
    sug = view.suggestion
    draft = view.draft

    widgets.append(_text(f"<b>✉ {sug.summary}</b>"))

    if not draft:
        widgets.append(_text("<i>Draft is being generated… tap Refresh to check.</i>"))
        widgets.append(
            _buttons(
                _button("↻ Refresh", "show_suggestions_tab"),
                _dismiss_button(sug.id),
            )
        )
        return widgets

    is_fwd = draft.is_forward
    pending = draft.pending_jit_data or {}
    needs_recruiter, needs_client = _missing_recipient_role(view)
    needs_cm = view.client_manager_email is None

    # ---- Recipients --------------------------------------------------
    if draft.to_emails:
        widgets.append(_decorated(", ".join(draft.to_emails), "To"))
        if draft.cc_emails:
            widgets.append(_decorated(", ".join(draft.cc_emails), "CC"))
    elif needs_recruiter:
        widgets.extend(_render_recruiter_jit(sug.id, draft.id, pending))
        known = _format_known_actors(view, exclude="recruiter")
        if known:
            widgets.append(_text(f'<font color="#888888"><small>{known}</small></font>'))
    elif needs_client:
        widgets.extend(_render_client_jit(sug.id, draft.id, pending))
        known = _format_known_actors(view, exclude="client_contact")
        if known:
            widgets.append(_text(f'<font color="#888888"><small>{known}</small></font>'))

    # ---- CM JIT (independent of TO/recipient role) ------------------
    # Always offered when the loop has no CM, so the coordinator can
    # supply someone to CC. Optional — does not gate Send.
    if needs_cm:
        widgets.extend(_render_cm_jit(sug.id, draft.id, pending))

    widgets.append(_decorated(draft.subject, "Subject"))
    widgets.append(_divider())

    # Editable body
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

    # Send is enabled when the required role (recruiter for NEW, client
    # otherwise) is either already on the loop OR staged in pending_jit_data.
    # CM is optional; it doesn't gate Send.
    send_disabled = False
    if not draft.to_emails:
        if needs_recruiter and not pending.get("recruiter", {}).get("email"):
            send_disabled = True
        if needs_client and not pending.get("client_contact", {}).get("email"):
            send_disabled = True

    send_label = "Forward" if is_fwd else "Send"
    send_required = [] if is_fwd else [input_name]
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
    return widgets


def _render_recruiter_jit(sug_id: str, draft_id: str, pending: dict) -> list[Widget]:
    """JIT inputs (or "selected" badge) for recruiter."""
    selected = pending.get("recruiter") or {}
    if selected.get("email"):
        return _render_jit_selected(
            label="Recruiter",
            name=selected.get("name") or selected["email"],
            email=selected["email"],
            sug_id=sug_id,
            draft_id=draft_id,
            role="recruiter",
        )
    widgets: list[Widget] = [
        _text("<i>Add the recruiter so this draft can be sent.</i>"),
    ]
    widgets.extend(
        build_recruiter_inputs(
            action_url=get_action_url(),
            directory_search_url=directory_search_url(),
            name_field=f"jit_recruiter_name_{sug_id}",
            email_field=f"jit_recruiter_email_{sug_id}",
            on_change_extra_params={
                "suggestion_id": sug_id,
                "draft_id": draft_id,
                "jit_role": "recruiter",
            },
        )
    )
    return widgets


def _render_client_jit(sug_id: str, draft_id: str, pending: dict) -> list[Widget]:
    """JIT inputs (or "selected" badge) for client contact."""
    selected = pending.get("client_contact") or {}
    if selected.get("email"):
        return _render_jit_selected(
            label="Client contact",
            name=selected.get("name") or selected["email"],
            email=selected["email"],
            sug_id=sug_id,
            draft_id=draft_id,
            role="client_contact",
        )
    widgets: list[Widget] = [
        _text("<i>Add the client contact so this draft can be sent.</i>"),
    ]
    widgets.extend(
        build_client_inputs(
            name_field=f"jit_client_name_{sug_id}",
            email_field=f"jit_client_email_{sug_id}",
            company_field=f"jit_client_company_{sug_id}",
        )
    )
    return widgets


def _render_cm_jit(sug_id: str, draft_id: str, pending: dict) -> list[Widget]:
    """JIT inputs (or "selected" badge) for client manager. Optional."""
    selected = pending.get("client_manager") or {}
    if selected.get("email"):
        return _render_jit_selected(
            label="CM (CC)",
            name=selected.get("name") or selected["email"],
            email=selected["email"],
            sug_id=sug_id,
            draft_id=draft_id,
            role="client_manager",
        )
    widgets: list[Widget] = [
        _text("<i>No client manager on this loop — add one to CC, or send without.</i>"),
    ]
    # Reuse the same Workspace-directory autocomplete (CMs are LRP folks).
    widgets.extend(
        build_recruiter_inputs(
            action_url=get_action_url(),
            directory_search_url=directory_search_url(),
            name_field=f"jit_cm_name_{sug_id}",
            email_field=f"jit_cm_email_{sug_id}",
            on_change_extra_params={
                "suggestion_id": sug_id,
                "draft_id": draft_id,
                "jit_role": "client_manager",
            },
        )
    )
    return widgets


def _render_jit_selected(
    *,
    label: str,
    name: str,
    email: str,
    sug_id: str,
    draft_id: str,
    role: str,
) -> list[Widget]:
    """Show a staged JIT pick with a small "x" button to clear it.

    The pick lives on ``draft.pending_jit_data[role]`` and isn't committed
    to the loop until Send. The "x" button hits ``clear_jit`` which wipes
    that role and re-renders the empty inputs.
    """
    clear_button = Button(
        text="✕ Clear",
        on_click=_action(
            "clear_jit",
            draft_id=draft_id,
            suggestion_id=sug_id,
            jit_role=role,
        ),
    )
    return [
        _decorated(f"{name} <{email}>", label),
        _buttons(clear_button),
    ]


def _build_ask_suggestion(view: SuggestionView) -> list[Widget]:
    """ASK_COORDINATOR - question + text input + disabled respond."""
    sug = view.suggestion
    widgets: list[Widget] = [
        _text("<b>❓ Agent needs clarification</b>"),
    ]

    question = (sug.action_data or {}).get("question")
    if question:
        widgets.append(_text(f'"{question}"'))

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
    action_data = sug.action_data or {}
    sid = sug.id

    def _val(key: str, default: str = "") -> str:
        return action_data.get(key) or default

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
    target_stage = (sug.action_data or {}).get("target_stage")
    current_label = _format_state_label(view.loop_state) if view.loop_state else None
    target_label = _format_state_label(target_stage) if target_stage else None
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


_MAX_SUGGESTIONS_PER_GROUP = 5
_MAX_TOTAL_SUGGESTIONS = 15


def build_overview(
    groups: list[LoopSuggestionGroup],
    base_url: str | None = None,
) -> CardResponse:
    """Build the main suggestion-centric overview card.

    Each loop group becomes a Section. Standalone suggestions (no loop)
    are rendered in a headerless section at the top.

    Google enforces a 28KB response size limit on card JSON, so we cap
    the number of rendered suggestions per group and globally.
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

    total_rendered = 0

    for group in groups:
        # Build all widgets for suggestions in this group
        group_widgets: list[Widget] = []

        # Candidate rename (only for placeholder names) appears at the top
        # of the section, before any suggestion widgets.
        rename_widgets = _build_candidate_rename(group)
        if rename_widgets:
            group_widgets.extend(rename_widgets)
            group_widgets.append(_divider())

        group_rendered = 0
        group_total = len(group.suggestions)

        for i, view in enumerate(group.suggestions):
            if group_rendered >= _MAX_SUGGESTIONS_PER_GROUP:
                break
            if total_rendered >= _MAX_TOTAL_SUGGESTIONS:
                break
            if i > 0:
                group_widgets.append(_divider())
            group_widgets.extend(_build_suggestion_widgets(view))
            group_rendered += 1
            total_rendered += 1

        hidden = group_total - group_rendered
        if hidden > 0:
            msg = f"{hidden} more — dismiss above to see more."
            group_widgets.append(_divider())
            group_widgets.append(_text(f'<font color="#888888"><i>{msg}</i></font>'))

        # Section header: loop title or "Unassigned" for loop-less
        header = None
        if group.loop_id and group.loop_title:
            header = group.loop_title
        elif group.loop_id:
            # Loop exists but no title - use candidate/company if available
            parts = [p for p in [group.candidate_name, group.client_company] if p]
            header = ", ".join(parts) if parts else f"Loop {group.loop_id[:8]}"

        sections.append(Section(header=header, widgets=group_widgets))

        if total_rendered >= _MAX_TOTAL_SUGGESTIONS:
            remaining_groups = len(groups) - (groups.index(group) + 1)
            if remaining_groups > 0:
                msg = f"{remaining_groups} more loop(s) not shown — dismiss above to see more."
                sections.append(
                    Section(widgets=[_text(f'<font color="#888888"><i>{msg}</i></font>')])
                )
            break

    return _update_card(Card(sections=sections))
