"""Card builder functions for agent suggestion views.

Builds Gmail sidebar cards showing agent suggestions, draft previews,
and action buttons for coordinator review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    Section,
    TextInput,
    TextInputWidget,
    TextParagraph,
    TextParagraphWidget,
    UpdateCard,
)

if TYPE_CHECKING:
    from api.agent.service import Suggestion, SuggestionDraft

from api.scheduling.cards import get_action_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLASSIFICATION_LABELS = {
    "new_interview_request": "New interview request",
    "availability_response": "Availability response received",
    "time_confirmation": "Time confirmed",
    "reschedule_request": "Reschedule requested",
    "cancellation": "Cancellation",
    "follow_up_needed": "Follow-up needed",
    "informational": "Informational — no action needed",
    "unrelated": "Not scheduling-related",
}

_ACTION_LABELS = {
    "draft_to_recruiter": "Send availability request to recruiter",
    "draft_to_client": "Send availability to client",
    "draft_confirmation": "Send confirmation",
    "draft_follow_up": "Send follow-up",
    "request_new_availability": "Request new availability",
    "mark_cold": "Mark as cold",
    "create_loop": "Create new scheduling loop",
    "ask_coordinator": "Agent needs your input",
    "no_action": "No action needed",
}


def _suggestion_action(
    action_name: str,
    **params: str,
) -> OnClick:
    """Build an OnClick for suggestion actions."""
    all_params = {"action_name": action_name, **params}
    parameters = [ActionParameter(key=k, value=v) for k, v in all_params.items()]
    return OnClick(
        action=OnClickAction(
            function=get_action_url(),
            parameters=parameters,
        )
    )


# ---------------------------------------------------------------------------
# Suggestion card for on-message (contextual view)
# ---------------------------------------------------------------------------


def build_suggestion_card(
    suggestion: Suggestion,
    draft: SuggestionDraft | None = None,
    loop_title: str | None = None,
) -> CardResponse:
    """Build a contextual card showing the agent's suggestion for a thread."""
    classification_label = _CLASSIFICATION_LABELS.get(
        suggestion.classification, suggestion.classification
    )
    action_label = _ACTION_LABELS.get(suggestion.suggested_action, suggestion.suggested_action)

    # Header
    header_subtitle = loop_title or "Agent Suggestion"
    confidence_indicator = "" if suggestion.confidence >= 0.7 else " ⚠️"
    header = CardHeader(
        title=f"🤖 {action_label}{confidence_indicator}",
        subtitle=header_subtitle,
    )

    # Classification section
    widgets = [
        DecoratedTextWidget(
            decorated_text=DecoratedText(
                top_label="Classification",
                text=classification_label,
            )
        ),
    ]

    # Questions from agent (ask_coordinator)
    if suggestion.questions:
        questions_text = "\n".join(f"• {q}" for q in suggestion.questions)
        widgets.append(
            TextParagraphWidget(
                text_paragraph=TextParagraph(text=f"<b>Questions:</b>\n{questions_text}")
            )
        )

    classification_section = Section(header="Analysis", widgets=widgets)

    sections = [classification_section]

    # Draft preview section (if draft exists)
    if draft:
        draft_widgets = [
            DecoratedTextWidget(
                decorated_text=DecoratedText(
                    top_label="To",
                    text=", ".join(draft.draft_to),
                )
            ),
            DecoratedTextWidget(
                decorated_text=DecoratedText(
                    top_label="Subject",
                    text=draft.draft_subject,
                )
            ),
            DividerWidget(divider={}),
            TextParagraphWidget(text_paragraph=TextParagraph(text=draft.draft_body)),
        ]
        sections.append(Section(header="Draft Email", widgets=draft_widgets))

    # Action buttons
    buttons = _build_action_buttons(suggestion, has_draft=draft is not None)
    sections.append(Section(widgets=[ButtonListWidget(button_list=ButtonList(buttons=buttons))]))

    # Collapsible reasoning
    if suggestion.reasoning:
        sections.append(
            Section(
                header="Agent Reasoning",
                collapsible=True,
                widgets=[
                    TextParagraphWidget(text_paragraph=TextParagraph(text=suggestion.reasoning))
                ],
            )
        )

    card = Card(header=header, sections=sections)
    return CardResponse(action=ActionResponse(navigations=[UpdateCard(update_card=card)]))


def _build_action_buttons(suggestion: Suggestion, *, has_draft: bool) -> list[Button]:
    """Build action buttons based on suggestion type."""
    buttons = []
    sid = suggestion.id

    if has_draft:
        buttons.append(
            Button(
                text="Send As-Is",
                on_click=_suggestion_action("approve_suggestion", suggestion_id=sid),
            )
        )
        buttons.append(
            Button(
                text="Edit",
                on_click=_suggestion_action("edit_suggestion", suggestion_id=sid),
            )
        )
    elif suggestion.suggested_action == "create_loop":
        buttons.append(
            Button(
                text="Create Loop",
                on_click=_suggestion_action("accept_create_loop", suggestion_id=sid),
            )
        )
    elif suggestion.suggested_action == "ask_coordinator":
        buttons.append(
            Button(
                text="Answer",
                on_click=_suggestion_action("answer_suggestion", suggestion_id=sid),
            )
        )
    elif suggestion.suggested_action == "mark_cold":
        buttons.append(
            Button(
                text="Mark Cold",
                on_click=_suggestion_action("approve_suggestion", suggestion_id=sid),
            )
        )

    # Always show dismiss
    buttons.append(
        Button(
            text="Dismiss",
            on_click=_suggestion_action("reject_suggestion", suggestion_id=sid),
        )
    )
    return buttons


# ---------------------------------------------------------------------------
# Pending suggestions list (for homepage Actions tab)
# ---------------------------------------------------------------------------


def build_pending_suggestions_section(
    suggestions: list[Suggestion],
) -> Section | None:
    """Build a section showing pending agent suggestions for the Actions tab.

    Returns None if there are no pending suggestions.
    """
    if not suggestions:
        return None

    widgets = []
    for s in suggestions:
        action_label = _ACTION_LABELS.get(s.suggested_action, s.suggested_action)
        confidence_indicator = "" if s.confidence >= 0.7 else " ⚠️"

        widgets.append(
            DecoratedTextWidget(
                decorated_text=DecoratedText(
                    top_label=f"🤖 {action_label}{confidence_indicator}",
                    text=s.classification.replace("_", " ").title(),
                    on_click=_suggestion_action("view_suggestion", suggestion_id=s.id),
                )
            )
        )

    return Section(
        header=f"🤖 Agent Suggestions ({len(suggestions)})",
        widgets=widgets,
    )


# ---------------------------------------------------------------------------
# Agent unavailable banner
# ---------------------------------------------------------------------------


def build_agent_unavailable_section() -> Section:
    """Build a section indicating the agent is unavailable."""
    return Section(
        widgets=[
            TextParagraphWidget(
                text_paragraph=TextParagraph(
                    text=(
                        "<b>Agent unavailable</b> — manual workflow active.\n"
                        "The agent will resume when the service recovers."
                    )
                )
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Answer form (for ask_coordinator suggestions)
# ---------------------------------------------------------------------------


def build_answer_form(suggestion: Suggestion) -> CardResponse:
    """Build a form for the coordinator to answer agent questions."""
    questions_text = "\n".join(f"• {q}" for q in suggestion.questions)

    widgets = [
        TextParagraphWidget(
            text_paragraph=TextParagraph(text=f"<b>The agent is asking:</b>\n{questions_text}")
        ),
        TextInputWidget(
            text_input=TextInput(
                name="coordinator_answer",
                label="Your answer",
                type="MULTIPLE_LINE",
            )
        ),
    ]

    buttons = [
        Button(
            text="Submit Answer",
            on_click=_suggestion_action(
                "submit_answer",
                suggestion_id=suggestion.id,
            ),
        ),
        Button(
            text="Cancel",
            on_click=_suggestion_action("show_drafts_tab"),
        ),
    ]
    widgets.append(ButtonListWidget(button_list=ButtonList(buttons=buttons)))

    card = Card(
        header=CardHeader(title="Agent Question", subtitle="Needs your input"),
        sections=[Section(widgets=widgets)],
    )
    return CardResponse(action=ActionResponse(navigations=[UpdateCard(update_card=card)]))
