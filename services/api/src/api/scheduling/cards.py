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
    TextInput,
    TextInputWidget,
    TextParagraph,
    TextParagraphWidget,
    UpdateCard,
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


def get_action_url() -> str:
    """Public accessor for the action URL (used by other card modules)."""
    return _action_url


def directory_search_url() -> str:
    """Derive the /addon/directory/search URL from the current action URL.

    Both live under /addon/ on the same host, so swap the last segment.
    Empty string when set_action_url hasn't been called (e.g. unit tests
    that don't exercise HTTP).
    """
    if not _action_url:
        return ""
    return _action_url.rsplit("/addon/", 1)[0] + "/addon/directory/search"


def _directory_autocomplete_action() -> OnClickAction:
    """Build the autoCompleteAction that fires per-keystroke on recruiter fields."""
    return OnClickAction(function=directory_search_url())


def _recruiter_selected_action() -> OnClickAction:
    """Build the onChangeAction that parses "Name <email>" into peer fields."""
    return OnClickAction(
        function=_action_url,
        parameters=[ActionParameter(key="action_name", value="recruiter_selected")],
    )


LRP_HEADER = CardHeader(
    title="LRP Scheduling",
    subtitle="Long Ridge Partners",
)


def _action(
    action_name: str,
    required_widgets: list[str] | None = None,
    load_indicator: str | None = None,
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
            load_indicator=load_indicator,
        )
    )


def _button(
    text: str,
    action_name: str,
    required_widgets: list[str] | None = None,
    load_indicator: str | None = None,
    **params: str,
) -> Button:
    return Button(
        text=text,
        on_click=_action(
            action_name,
            required_widgets=required_widgets,
            load_indicator=load_indicator,
            **params,
        ),
    )


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
                            _button(
                                "Create New Loop",
                                "show_create_form",
                                load_indicator="SPINNER",
                                **btn_params,
                            ),
                        ),
                    ]
                ),
            ],
        )
    )


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
    gmail_message_id: str | None = None,
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
    banner: str | None = None,
    suggestion_id: str | None = None,
) -> CardResponse:
    sections = []

    # Informational banner (when the AI extractor contributed prefills).
    # Rendered above any error banner so the coordinator sees context first.
    if banner:
        sections.append(
            Section(
                widgets=[
                    TextParagraphWidget(text_paragraph=TextParagraph(text=f"<i>{banner}</i>")),
                ]
            )
        )

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

    # Recruiter section — directory-backed autocomplete on both fields.
    # Coordinators can type into either the name OR email input and pick a
    # Workspace member from the live dropdown. onChangeAction fires after
    # selection; the handler parses "Name <email>" and re-renders this
    # form with the two halves in their respective fields.
    recruiter_autocomplete = _directory_autocomplete_action()
    recruiter_onchange = _recruiter_selected_action()
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
                        hint_text="Type to search your Workspace directory",
                        auto_complete_action=recruiter_autocomplete,
                        on_change_action=recruiter_onchange,
                    )
                ),
                TextInputWidget(
                    text_input=TextInput(
                        name="recruiter_email",
                        label="Email",
                        type="SINGLE_LINE",
                        value=prefill_recruiter_email,
                        auto_complete_action=recruiter_autocomplete,
                        on_change_action=recruiter_onchange,
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
            uncollapsible_widgets_count=1,
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
    if gmail_message_id:
        params["gmail_message_id"] = gmail_message_id
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
