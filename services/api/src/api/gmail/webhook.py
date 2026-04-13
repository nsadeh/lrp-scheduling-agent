"""Authenticated Gmail Pub/Sub webhook endpoint.

Receives push notifications from Google Cloud Pub/Sub, verifies the
OIDC bearer token, and enqueues an arq job for background processing.
"""

from __future__ import annotations

import base64
import json
import logging
import os

from fastapi import APIRouter, Request, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["gmail-push"])

PUBSUB_SERVICE_ACCOUNT = os.environ.get(
    "PUBSUB_SERVICE_ACCOUNT",
    "gmail-api-push@system.gserviceaccount.com",
)
EXPECTED_AUDIENCE = os.environ.get("PUBSUB_WEBHOOK_AUDIENCE", "")
if not EXPECTED_AUDIENCE:
    logger.warning(
        "PUBSUB_WEBHOOK_AUDIENCE not set — webhook OIDC audience "
        "validation is disabled. Set this in production."
    )


async def _verify_pubsub_token(request: Request) -> dict:
    """Verify the OIDC token Google attaches to Pub/Sub push messages.

    Returns the verified claims dict, or raises ValueError on failure.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise ValueError("Missing bearer token")

    token = auth_header[7:]
    claims = id_token.verify_oauth2_token(
        token,
        google_requests.Request(),
        audience=EXPECTED_AUDIENCE or None,
    )

    if claims.get("email") != PUBSUB_SERVICE_ACCOUNT:
        raise ValueError(f"Unexpected sender: {claims.get('email')}")

    return claims


@webhook_router.post("/webhook/gmail")
async def gmail_webhook(request: Request) -> Response:
    """Receive Gmail Pub/Sub push notifications.

    1. Verify OIDC bearer token (Google-signed)
    2. Parse emailAddress + historyId from Pub/Sub data
    3. Validate coordinator has authorized the app
    4. Enqueue arq job for background processing
    5. Always return 200 (prevents Pub/Sub retries on errors)
    """
    # Verify OIDC token
    try:
        await _verify_pubsub_token(request)
    except (ValueError, Exception) as exc:
        logger.warning("webhook auth failed: %s", exc)
        # Return 200 even on auth failure to prevent Pub/Sub retries
        # from hammering the endpoint. Log for monitoring.
        return Response(status_code=200)

    # Parse Pub/Sub message
    try:
        body = await request.json()
        message_data = body.get("message", {}).get("data", "")
        decoded = base64.b64decode(message_data).decode("utf-8")
        notification = json.loads(decoded)
        coordinator_email = notification.get("emailAddress", "")
        history_id = str(notification.get("historyId", ""))
    except Exception:
        logger.exception("webhook parse error")
        return Response(status_code=200)

    if not coordinator_email or not history_id:
        logger.warning("webhook missing emailAddress or historyId")
        return Response(status_code=200)

    # Check if we have credentials for this coordinator
    token_store = request.app.state.gmail._token_store
    if not await token_store.has_token(coordinator_email):
        logger.debug("webhook for unknown coordinator: %s", coordinator_email)
        return Response(status_code=200)

    # Enqueue background job
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        logger.warning("redis not available — cannot enqueue push job")
        return Response(status_code=200)

    try:
        await redis.enqueue_job(
            "process_gmail_push",
            coordinator_email,
            history_id,
        )
        logger.info(
            "enqueued push job coordinator=%s history_id=%s",
            coordinator_email,
            history_id,
        )
    except Exception:
        logger.exception("failed to enqueue push job for %s", coordinator_email)

    return Response(status_code=200)
