"""Card builder functions for AI email draft views.

Pure functions returning CardResponse models for the Gmail sidebar.
Reuses the shared helpers from scheduling/cards.py.
"""

from __future__ import annotations

from api.addon.models import (
    Card,
    CardHeader,
    CardResponse,
    Section,
    TextInput,
    TextInputWidget,
)
from api.drafts.models import EmailDraft  # noqa: TC001 — needed at runtime
from api.scheduling.cards import (
    _button,
    _buttons,
    _decorated,
    _divider,
    _text,
    _update_card,
)


def build_draft_preview(draft: EmailDraft) -> CardResponse:
    """Read-only draft preview with Send / Edit / Discard buttons."""
    widgets = []

    # Recipients
    widgets.append(_decorated(", ".join(draft.to_emails), "To"))
    if draft.cc_emails:
        widgets.append(_decorated(", ".join(draft.cc_emails), "CC"))
    widgets.append(_decorated(draft.subject, "Subject"))
    widgets.append(_divider())

    # Body
    if draft.body:
        widgets.append(_text(draft.body))
    else:
        widgets.append(_text("<i>No draft body generated — compose manually below.</i>"))

    widgets.append(_divider())

    # Action buttons
    widgets.append(
        _buttons(
            _button("Edit & Send", "edit_draft", draft_id=draft.id),
            _button("Send Now", "send_draft", draft_id=draft.id),
            _button("Discard", "discard_draft", draft_id=draft.id),
        )
    )

    return _update_card(
        Card(
            header=CardHeader(title="AI Draft", subtitle=draft.subject),
            sections=[Section(widgets=widgets)],
        )
    )


def build_draft_edit(draft: EmailDraft) -> CardResponse:
    """Editable draft form with TextInput for body, Send + Cancel buttons."""
    widgets = []

    # Read-only info
    widgets.append(_decorated(", ".join(draft.to_emails), "To"))
    if draft.cc_emails:
        widgets.append(_decorated(", ".join(draft.cc_emails), "CC"))
    widgets.append(_divider())

    # Editable body
    widgets.append(
        TextInputWidget(
            text_input=TextInput(
                name="draft_body",
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
                required_widgets=["draft_body"],
                draft_id=draft.id,
            ),
            _button("Cancel", "view_draft", draft_id=draft.id),
        )
    )

    return _update_card(
        Card(
            header=CardHeader(title="Edit Draft", subtitle=draft.subject),
            sections=[Section(widgets=widgets)],
        )
    )


def build_drafts_list_sections(drafts: list[EmailDraft]) -> list[Section]:
    """Build sections for pending AI drafts to insert into the drafts tab.

    Returns one section with a header + one decorated text widget per draft.
    Returns an empty list if no drafts are pending.
    """
    if not drafts:
        return []

    widgets = []
    widgets.append(_text("<b>AI Drafts</b>"))

    for draft in drafts:
        # Show recipient + subject as a clickable item
        recipient = ", ".join(draft.to_emails) if draft.to_emails else "Unknown"
        label = f"To: {recipient}"
        widgets.append(
            _decorated(
                text=f"<b>{draft.subject}</b>",
                top_label=label,
            )
        )
        widgets.append(
            _buttons(
                _button("Review", "view_draft", draft_id=draft.id),
                _button("Send", "send_draft", draft_id=draft.id),
                _button("Discard", "discard_draft", draft_id=draft.id),
            )
        )

    widgets.append(_divider())
    return [Section(widgets=widgets)]
