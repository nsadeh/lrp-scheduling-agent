"""Google Workspace Add-on HTTP endpoints.

Google POSTs to these routes when a coordinator interacts with the Gmail sidebar.
Token verification is applied at the router level via Depends().
"""

from fastapi import APIRouter, Depends, Request

from api.addon.auth import verify_google_addon_token
from api.addon.cards import build_homepage_card, build_message_card
from api.addon.models import AddonRequest

addon_router = APIRouter(
    prefix="/addon",
    tags=["addon"],
    dependencies=[Depends(verify_google_addon_token)],
)


@addon_router.post("/homepage")
async def addon_homepage(body: AddonRequest, request: Request) -> dict:
    """Homepage trigger — fired when the add-on icon is clicked with no message open."""
    card = build_homepage_card()
    return card.model_dump(by_alias=True, exclude_none=True)


@addon_router.post("/on-message")
async def addon_on_message(body: AddonRequest, request: Request) -> dict:
    """Contextual trigger — fired when a coordinator has an email message open."""
    message_id = None
    if body.gmail:
        message_id = body.gmail.message_id

    card = build_homepage_card() if not message_id else build_message_card(message_id=message_id)
    return card.model_dump(by_alias=True, exclude_none=True)
