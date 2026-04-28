"""Shared builders for recruiter / client contact input widgets.

Used by both the manual create-loop form and the JIT collection on draft
cards (when a draft needs to send to a recruiter the loop doesn't have
yet, the draft card asks for it inline using the same autocomplete UI).

The recruiter inputs are wired to the Workspace directory autocomplete
endpoint at /addon/directory/search; selecting an entry triggers the
`recruiter_selected` action which parses "Name <email>" and re-renders
the host card with both fields populated.
"""

from __future__ import annotations

from api.addon.models import (
    ActionParameter,
    OnClickAction,
    TextInput,
    TextInputWidget,
)


def build_recruiter_inputs(
    *,
    action_url: str,
    directory_search_url: str,
    name_field: str,
    email_field: str,
    prefill_name: str | None = None,
    prefill_email: str | None = None,
    on_change_extra_params: dict[str, str] | None = None,
) -> list[TextInputWidget]:
    """Two TextInputs (name + email) wired to directory autocomplete.

    The two inputs share auto_complete_action (per-keystroke directory
    search) and on_change_action (parses "Name <email>" and refreshes the
    card). Pass `on_change_extra_params` when the host card needs extra
    context to re-render after selection (e.g. suggestion_id, draft_id).
    """
    extra = on_change_extra_params or {}
    on_change_params = [
        ActionParameter(key="action_name", value="recruiter_selected"),
        *[ActionParameter(key=k, value=v) for k, v in extra.items()],
    ]
    autocomplete = OnClickAction(function=directory_search_url) if directory_search_url else None
    on_change = OnClickAction(function=action_url, parameters=on_change_params)

    return [
        TextInputWidget(
            text_input=TextInput(
                name=name_field,
                label="Name",
                type="SINGLE_LINE",
                value=prefill_name,
                hint_text="Type to search your Workspace directory",
                auto_complete_action=autocomplete,
                on_change_action=on_change,
            )
        ),
        TextInputWidget(
            text_input=TextInput(
                name=email_field,
                label="Email",
                type="SINGLE_LINE",
                value=prefill_email,
                auto_complete_action=autocomplete,
                on_change_action=on_change,
            )
        ),
    ]


def build_client_inputs(
    *,
    name_field: str,
    email_field: str,
    company_field: str | None = None,
    prefill_name: str | None = None,
    prefill_email: str | None = None,
    prefill_company: str | None = None,
) -> list[TextInputWidget]:
    """Three TextInputs for the client contact (name + email + optional company).

    No directory autocomplete — clients are external. Company is optional
    now that `client_contacts.company` is nullable.
    """
    widgets: list[TextInputWidget] = [
        TextInputWidget(
            text_input=TextInput(
                name=name_field,
                label="Contact Name",
                type="SINGLE_LINE",
                value=prefill_name,
            )
        ),
        TextInputWidget(
            text_input=TextInput(
                name=email_field,
                label="Contact Email",
                type="SINGLE_LINE",
                value=prefill_email,
            )
        ),
    ]
    if company_field:
        widgets.append(
            TextInputWidget(
                text_input=TextInput(
                    name=company_field,
                    label="Company",
                    type="SINGLE_LINE",
                    value=prefill_company,
                )
            )
        )
    return widgets
