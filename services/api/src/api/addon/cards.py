"""Card builder functions for Google Workspace Add-on responses.

Pure functions that return Pydantic CardResponse models.
The LRP logo is embedded as a base64 data URI to avoid image-fetching issues
with ngrok's free-tier interstitial page.
"""

from pathlib import Path

from api.addon.models import (
    ActionResponse,
    Card,
    CardHeader,
    CardResponse,
    Section,
    TextParagraph,
    TextParagraphWidget,
    UpdateCard,
)


def _load_logo_data_uri() -> str:
    """Load the LRP logo as a base64 data URI from the static directory."""
    import base64

    logo_path = Path(__file__).resolve().parent.parent.parent.parent / "static" / "lrp-logo.png"
    if logo_path.exists():
        b64 = base64.b64encode(logo_path.read_bytes()).decode()
        return f"data:image/png;base64,{b64}"
    return ""


LRP_LOGO_DATA_URI = _load_logo_data_uri()


def build_homepage_card() -> CardResponse:
    """Build the homepage card shown when the add-on icon is clicked (no message open)."""
    return CardResponse(
        action=ActionResponse(
            navigations=[
                UpdateCard(
                    update_card=Card(
                        header=CardHeader(
                            title="LRP Scheduling Agent",
                            subtitle="Long Ridge Partners",
                            image_url=LRP_LOGO_DATA_URI or None,
                            image_type="CIRCLE" if LRP_LOGO_DATA_URI else None,
                        ),
                        sections=[
                            Section(
                                widgets=[
                                    TextParagraphWidget(
                                        text_paragraph=TextParagraph(
                                            text="Open a message to see scheduling options."
                                        )
                                    ),
                                ]
                            )
                        ],
                    )
                )
            ]
        )
    )


def build_message_card(message_id: str) -> CardResponse:
    """Build the contextual card shown when a coordinator has a message open."""
    return CardResponse(
        action=ActionResponse(
            navigations=[
                UpdateCard(
                    update_card=Card(
                        header=CardHeader(
                            title="LRP Scheduling Agent",
                            subtitle="Long Ridge Partners",
                            image_url=LRP_LOGO_DATA_URI or None,
                            image_type="CIRCLE" if LRP_LOGO_DATA_URI else None,
                        ),
                        sections=[
                            Section(
                                header="Message Context",
                                widgets=[
                                    TextParagraphWidget(
                                        text_paragraph=TextParagraph(
                                            text=f"Viewing message: <b>{message_id}</b>"
                                        )
                                    ),
                                    TextParagraphWidget(
                                        text_paragraph=TextParagraph(
                                            text="Scheduling features coming soon."
                                        )
                                    ),
                                ],
                            )
                        ],
                    )
                )
            ]
        )
    )
