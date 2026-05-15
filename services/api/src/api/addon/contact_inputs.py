"""Shared builders for recruiter / client contact input widgets.

Used by both the manual create-loop form and the JIT collection on draft
cards (when a draft needs to send to a recruiter the loop doesn't have
yet, the draft card asks for it inline using the same autocomplete UI).

The recruiter inputs use the Workspace directory autocomplete endpoint at
/addon/directory/search, which returns "Name <email>" entries. Selecting
one natively populates the focused input — there is intentionally NO
on_change action: an onChange round-trip forced a full card rebuild
(reloading every suggestion across every thread, the bug this avoids).
The commit handlers (UPDATE_ACTOR Save, JIT Send) already parse
"Name <email>" out of either field, so no server-side split is needed.
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
    directory_search_url: str,
    name_field: str,
    email_field: str,
    prefill_name: str | None = None,
    prefill_email: str | None = None,
) -> list[TextInputWidget]:
    """Two TextInputs (name + email) wired to directory autocomplete.

    Both inputs carry auto_complete_action (per-keystroke directory search).
    Selecting an entry natively fills the focused field with "Name <email>";
    the commit handlers parse it from whichever field holds it. No
    on_change_action — see the module docstring for why.
    """

    def _autocomplete_for(field_name: str) -> OnClickAction | None:
        """Per-field autocomplete action carrying the field name as a parameter.

        Lets the server identify which field fired the autocomplete in
        scenarios where multiple directory inputs are on screen (e.g. one
        draft has a recruiter input mid-type while another has a CM input).
        """
        if not directory_search_url:
            return None
        return OnClickAction(
            function=directory_search_url,
            parameters=[ActionParameter(key="autocomplete_field", value=field_name)],
        )

    return [
        TextInputWidget(
            text_input=TextInput(
                name=name_field,
                label="Name",
                type="SINGLE_LINE",
                value=prefill_name,
                hint_text="Type to search your Workspace directory",
                auto_complete_action=_autocomplete_for(name_field),
            )
        ),
        TextInputWidget(
            text_input=TextInput(
                name=email_field,
                label="Email",
                type="SINGLE_LINE",
                value=prefill_email,
                auto_complete_action=_autocomplete_for(email_field),
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
