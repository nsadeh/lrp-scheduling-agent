"""Card builder functions for scheduling loop views.

Pure functions returning CardResponse models for the Gmail sidebar.
"""

from __future__ import annotations

from api.addon.models import (
    ActionParameter,
    ActionResponse,
    Button,
    ButtonList,
    ButtonListWidget,
    Card,
    CardHeader,
    CardResponse,
    DecoratedText,
    DecoratedTextWidget,
    DividerWidget,
    OnClick,
    OnClickAction,
    OpenLink,
    Section,
    SelectionInput,
    SelectionInputWidget,
    SelectionItem,
    TextInput,
    TextInputWidget,
    TextParagraph,
    TextParagraphWidget,
    UpdateCard,
)
from api.scheduling.models import (
    Loop,
    Stage,
    StageState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Set by the routes module at startup with the backend's public URL
_action_url: str = ""


def set_action_url(url: str) -> None:
    """Set the base URL for action callbacks (e.g. https://xxx.ngrok-free.app/addon/action)."""
    global _action_url
    _action_url = url


LRP_HEADER = CardHeader(
    title="LRP Scheduling",
    subtitle="Long Ridge Partners",
)


def _action(
    action_name: str,
    required_widgets: list[str] | None = None,
    **params: str,
) -> OnClick:
    """Build an OnClick that POSTs to our /addon/action endpoint.

    For HTTP-based add-ons, `function` must be a full HTTPS URL.
    The logical action name is passed as the `action_name` parameter.
    """
    all_params = {"action_name": action_name, **params}
    parameters = [ActionParameter(key=k, value=v) for k, v in all_params.items()]
    return OnClick(
        action=OnClickAction(
            function=_action_url,
            parameters=parameters,
            required_widgets=required_widgets,
        )
    )


def _button(
    text: str,
    action_name: str,
    required_widgets: list[str] | None = None,
    **params: str,
) -> Button:
    return Button(
        text=text,
        on_click=_action(action_name, required_widgets=required_widgets, **params),
    )


def _initials(name: str) -> str:
    """Extract initials from a name. 'Sarah Kim' → 'SK', 'Bob' → 'B'."""
    return "".join(part[0].upper() for part in name.split() if part)


def _text(content: str) -> TextParagraphWidget:
    return TextParagraphWidget(text_paragraph=TextParagraph(text=content))


def _decorated(text: str, top_label: str | None = None) -> DecoratedTextWidget:
    return DecoratedTextWidget(
        decorated_text=DecoratedText(text=text, top_label=top_label, wrap_text=True)
    )


def _buttons(*btns: Button) -> ButtonListWidget:
    return ButtonListWidget(button_list=ButtonList(buttons=list(btns)))


def _divider() -> DividerWidget:
    return DividerWidget()


def _update_card(card: Card) -> CardResponse:
    """Replace the current card in-place — never push onto the nav stack."""
    return CardResponse(action=ActionResponse(navigations=[UpdateCard(update_card=card)]))


def build_error_card(message: str) -> CardResponse:
    """Generic error card shown when an operation fails."""
    return _update_card(
        Card(
            header=CardHeader(title="Error"),
            sections=[
                Section(
                    widgets=[
                        TextParagraphWidget(text_paragraph=TextParagraph(text=f"<b>{message}</b>")),
                    ]
                ),
            ],
        )
    )


# ---------------------------------------------------------------------------
# Authorization Required
# ---------------------------------------------------------------------------


def build_auth_required(auth_url: str) -> CardResponse:
    """Card shown when the coordinator hasn't authorized Gmail access yet."""
    return _update_card(
        Card(
            header=LRP_HEADER,
            sections=[
                Section(
                    widgets=[
                        _text(
                            "To use the scheduling tool, you need to authorize "
                            "Gmail access. This allows the tool to read message "
                            "context and send emails on your behalf."
                        ),
                        ButtonListWidget(
                            button_list=ButtonList(
                                buttons=[
                                    Button(
                                        text="Authorize Gmail Access",
                                        on_click=OnClick(
                                            open_link=OpenLink(url=auth_url),
                                        ),
                                    )
                                ]
                            )
                        ),
                    ]
                ),
            ],
        )
    )


# ---------------------------------------------------------------------------
# Contextual View (Message Open)
# ---------------------------------------------------------------------------


def build_contextual_unlinked(gmail_thread_id: str, message_id: str | None = None) -> CardResponse:
    """Prompt to create or link when thread is not associated with a loop."""
    btn_params = {"gmail_thread_id": gmail_thread_id}
    if message_id:
        btn_params["message_id"] = message_id
    return _update_card(
        Card(
            sections=[
                Section(
                    widgets=[
                        _text("This thread is not linked to a scheduling loop."),
                        _buttons(
                            _button("Create New Loop", "show_create_form", **btn_params),
                        ),
                    ]
                ),
            ],
        )
    )


# ---------------------------------------------------------------------------
# Loop Detail
# ---------------------------------------------------------------------------


def _build_loop_header_title(loop: Loop) -> str:
    """Build header: 'CM/Recruiter initials, Stage, Candidate, Client'."""
    parts = []
    # Initials
    recruiter_init = _initials(loop.recruiter.name) if loop.recruiter else ""
    if loop.client_manager:
        parts.append(f"{_initials(loop.client_manager.name)}/{recruiter_init}")
    elif recruiter_init:
        parts.append(recruiter_init)
    # Most urgent stage
    urgent = loop.most_urgent_stage
    if urgent:
        parts.append(urgent.name)
    # Candidate
    if loop.candidate:
        parts.append(loop.candidate.name)
    # Client company
    if loop.client_contact:
        parts.append(loop.client_contact.company)
    return ", ".join(parts)


def build_loop_detail(loop: Loop) -> CardResponse:
    sections = []

    # Stages section
    for stage in loop.stages:
        stage_widgets = _build_stage_widgets(stage, loop)
        sections.append(
            Section(
                header=f"{stage.name} · {stage.state.replace('_', ' ').title()}",
                widgets=stage_widgets,
            )
        )

    # Add stage button
    sections.append(
        Section(widgets=[_buttons(_button("+ Add Stage", "add_stage", loop_id=loop.id))])
    )

    # Email threads section
    if loop.email_threads:
        thread_widgets = [
            _decorated(t.subject or "(no subject)", "Linked Thread") for t in loop.email_threads
        ]
        sections.append(
            Section(header=f"Email Threads ({len(loop.email_threads)})", widgets=thread_widgets)
        )

    # Edit loop button at the bottom
    sections.append(
        Section(widgets=[_buttons(_button("Edit Loop", "edit_actors", loop_id=loop.id))])
    )

    header = CardHeader(title=_build_loop_header_title(loop))
    return _update_card(Card(header=header, sections=sections))


def _build_stage_widgets(stage: Stage, loop: Loop) -> list:
    loop_id = loop.id
    widgets = []
    widgets.append(_decorated(stage.next_action, "Next Action"))

    # Time slots
    for ts in stage.time_slots:
        widgets.append(
            _decorated(
                f"{ts.start_time.strftime('%a %m/%d %I:%M %p')} ({ts.timezone})",
                "Scheduled",
            )
        )

    # Action buttons based on state
    buttons = []
    if stage.state == StageState.NEW:
        # Forward thread to recruiter (one-click, no body)
        buttons.append(
            _button("Forward to Recruiter", "forward_thread", stage_id=stage.id, loop_id=loop_id)
        )
    elif stage.state == StageState.AWAITING_CANDIDATE:
        # Inline email textarea — always visible
        widgets.append(
            TextInputWidget(
                text_input=TextInput(
                    name="email_body",
                    label="Message to Client",
                    type="MULTIPLE_LINE",
                    hint_text="Enter candidate availability to send to client",
                )
            )
        )
        buttons.append(
            _button(
                "Send Availability to Client",
                "send_inline_email",
                stage_id=stage.id,
                loop_id=loop_id,
            )
        )
    elif stage.state == StageState.AWAITING_CLIENT:
        buttons.append(
            _button(
                "Mark Scheduled", "advance_stage", stage_id=stage.id, to_state=StageState.SCHEDULED
            )
        )
        buttons.append(
            _button(
                "No Overlap — Retry",
                "advance_stage",
                stage_id=stage.id,
                to_state=StageState.AWAITING_CANDIDATE,
            )
        )
    elif stage.state == StageState.SCHEDULED:
        buttons.append(
            _button(
                "Mark Complete", "advance_stage", stage_id=stage.id, to_state=StageState.COMPLETE
            )
        )
        buttons.append(
            _button("Add Time Slot", "show_add_time_slot", stage_id=stage.id, loop_id=loop_id)
        )
    elif stage.state == StageState.COLD:
        buttons.append(_button("Revive", "show_revive", stage_id=stage.id, loop_id=loop_id))

    # Cold button for active stages
    if stage.state not in (StageState.COMPLETE, StageState.COLD):
        buttons.append(_button("Go Cold", "mark_cold", stage_id=stage.id))

    if buttons:
        widgets.append(_buttons(*buttons))

    return widgets


# ---------------------------------------------------------------------------
# Create Loop Form
# ---------------------------------------------------------------------------


_REQUIRED_CREATE_FIELDS = [
    "candidate_name",
    "client_name",
    "client_email",
    "client_company",
    "recruiter_name",
    "recruiter_email",
]


def build_create_loop_form(
    gmail_thread_id: str | None = None,
    gmail_subject: str | None = None,
    prefill_client_name: str | None = None,
    prefill_client_email: str | None = None,
    prefill_cm_name: str | None = None,
    prefill_cm_email: str | None = None,
    prefill_candidate_name: str | None = None,
    prefill_recruiter_name: str | None = None,
    prefill_recruiter_email: str | None = None,
    prefill_client_company: str | None = None,
    prefill_first_stage: str | None = None,
    error_message: str | None = None,
    suggestion_id: str | None = None,
) -> CardResponse:
    sections = []

    # Error banner (if any)
    if error_message:
        sections.append(
            Section(
                widgets=[
                    TextParagraphWidget(
                        text_paragraph=TextParagraph(
                            text=f'<b><font color="#cc0000">{error_message}</font></b>'
                        )
                    ),
                ]
            )
        )

    # Candidate section
    sections.append(
        Section(
            header="Candidate",
            widgets=[
                TextInputWidget(
                    text_input=TextInput(
                        name="candidate_name",
                        label="Candidate Name",
                        type="SINGLE_LINE",
                        value=prefill_candidate_name,
                    )
                ),
            ],
        )
    )

    # Client contact section
    sections.append(
        Section(
            header="Client Contact",
            widgets=[
                TextInputWidget(
                    text_input=TextInput(
                        name="client_name",
                        label="Contact Name",
                        type="SINGLE_LINE",
                        value=prefill_client_name,
                    )
                ),
                TextInputWidget(
                    text_input=TextInput(
                        name="client_email",
                        label="Contact Email",
                        type="SINGLE_LINE",
                        value=prefill_client_email,
                    )
                ),
                TextInputWidget(
                    text_input=TextInput(
                        name="client_company",
                        label="Company",
                        type="SINGLE_LINE",
                        value=prefill_client_company,
                    )
                ),
            ],
        )
    )

    # Recruiter section
    sections.append(
        Section(
            header="Recruiter",
            widgets=[
                TextInputWidget(
                    text_input=TextInput(
                        name="recruiter_name",
                        label="Name",
                        type="SINGLE_LINE",
                        value=prefill_recruiter_name,
                    )
                ),
                TextInputWidget(
                    text_input=TextInput(
                        name="recruiter_email",
                        label="Email",
                        type="SINGLE_LINE",
                        value=prefill_recruiter_email,
                    )
                ),
            ],
        )
    )

    # Client manager section (optional, collapsible)
    sections.append(
        Section(
            header="Client Manager (optional)",
            widgets=[
                TextInputWidget(
                    text_input=TextInput(
                        name="cm_name",
                        label="Name",
                        type="SINGLE_LINE",
                        value=prefill_cm_name,
                    )
                ),
                TextInputWidget(
                    text_input=TextInput(
                        name="cm_email",
                        label="Email",
                        type="SINGLE_LINE",
                        value=prefill_cm_email,
                    )
                ),
            ],
            collapsible=True,
            uncollapsible_widgets_count=0,
        )
    )

    # Stage section
    sections.append(
        Section(
            header="Stage",
            widgets=[
                TextInputWidget(
                    text_input=TextInput(
                        name="first_stage_name",
                        label="First Stage Name",
                        type="SINGLE_LINE",
                        value=prefill_first_stage or "Round 1",
                    )
                ),
            ],
        )
    )

    # Hidden params passed through
    params = {}
    if gmail_thread_id:
        params["gmail_thread_id"] = gmail_thread_id
    if gmail_subject:
        params["gmail_subject"] = gmail_subject
    if suggestion_id:
        params["suggestion_id"] = suggestion_id

    sections.append(
        Section(
            widgets=[
                _buttons(
                    _button(
                        "Create Loop",
                        "create_loop",
                        required_widgets=_REQUIRED_CREATE_FIELDS,
                        **params,
                    ),
                ),
            ]
        )
    )

    return _update_card(
        Card(
            header=CardHeader(title="New Scheduling Loop", subtitle="Enter details"),
            sections=sections,
        )
    )


# ---------------------------------------------------------------------------
# Compose Email
# ---------------------------------------------------------------------------


def build_compose_email(
    loop: Loop,
    stage: Stage,
    to_email: str,
    subject: str,
    gmail_thread_id: str | None = None,
) -> CardResponse:
    widgets = []

    widgets.append(_decorated(to_email, "To"))
    widgets.append(_decorated(subject, "Subject"))

    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name="email_body",
                label="Message",
                type="MULTIPLE_LINE",
                hint_text="Keep it short and professional",
            )
        )
    )

    params = {"stage_id": stage.id, "loop_id": loop.id, "to_email": to_email, "subject": subject}
    if gmail_thread_id:
        params["gmail_thread_id"] = gmail_thread_id

    widgets.append(
        _buttons(
            _button("Send", "send_email", **params),
            _button("Cancel", "view_loop", loop_id=loop.id),
        )
    )

    return _update_card(
        Card(
            header=CardHeader(
                title="Send Email",
                subtitle=f"{loop.title} · {stage.name}",
            ),
            sections=[Section(widgets=widgets)],
        )
    )


# ---------------------------------------------------------------------------
# Add Time Slot Form
# ---------------------------------------------------------------------------


def build_add_time_slot_form(stage: Stage, loop_id: str) -> CardResponse:
    widgets = [
        TextInputWidget(
            text_input=TextInput(name="date", label="Date (YYYY-MM-DD)", type="SINGLE_LINE")
        ),
        TextInputWidget(
            text_input=TextInput(name="time", label="Time (HH:MM)", type="SINGLE_LINE")
        ),
        TextInputWidget(
            text_input=TextInput(
                name="timezone", label="Timezone", type="SINGLE_LINE", value="America/New_York"
            )
        ),
        TextInputWidget(
            text_input=TextInput(
                name="duration", label="Duration (minutes)", type="SINGLE_LINE", value="60"
            )
        ),
        TextInputWidget(
            text_input=TextInput(name="zoom_link", label="Zoom Link (optional)", type="SINGLE_LINE")
        ),
        _buttons(
            _button("Save", "save_time_slot", stage_id=stage.id, loop_id=loop_id),
            _button("Cancel", "view_loop", loop_id=loop_id),
        ),
    ]

    return _update_card(
        Card(
            header=CardHeader(title="Add Time Slot", subtitle=stage.name),
            sections=[Section(widgets=widgets)],
        )
    )


# ---------------------------------------------------------------------------
# Revive Stage
# ---------------------------------------------------------------------------


def build_revive_form(stage: Stage, loop_id: str) -> CardResponse:
    widgets = [
        _text(f"Revive <b>{stage.name}</b> — choose which state to resume from:"),
        SelectionInputWidget(
            selection_input=SelectionInput(
                name="revive_to_state",
                label="Resume from",
                type="RADIO_BUTTON",
                items=[
                    SelectionItem(text="New (restart)", value=StageState.NEW),
                    SelectionItem(text="Awaiting Candidate", value=StageState.AWAITING_CANDIDATE),
                    SelectionItem(text="Awaiting Client", value=StageState.AWAITING_CLIENT),
                ],
            )
        ),
        _buttons(
            _button("Revive", "revive_stage", stage_id=stage.id, loop_id=loop_id),
            _button("Cancel", "view_loop", loop_id=loop_id),
        ),
    ]

    return _update_card(
        Card(
            header=CardHeader(title="Revive Stage", subtitle=stage.name),
            sections=[Section(widgets=widgets)],
        )
    )
