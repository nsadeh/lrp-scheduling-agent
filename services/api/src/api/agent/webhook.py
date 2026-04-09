"""Gmail Pub/Sub webhook endpoint.

Receives push notifications when new emails arrive in coordinator inboxes.
Validates the Pub/Sub message, then enqueues an arq job for processing.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["webhook"])


class PubSubMessage:
    """Parsed Pub/Sub push message."""

    def __init__(self, email_address: str, history_id: str):
        self.email_address = email_address
        self.history_id = history_id

    @classmethod
    def from_request_body(cls, body: dict[str, Any]) -> PubSubMessage | None:
        """Parse the Pub/Sub push message from the request body.

        Expected format:
        {
            "message": {
                "data": "<base64 encoded JSON>",
                "messageId": "...",
                "publishTime": "..."
            },
            "subscription": "projects/.../subscriptions/..."
        }

        The base64-decoded data contains:
        {
            "emailAddress": "coordinator@lrp.com",
            "historyId": "12345"
        }
        """
        try:
            message = body.get("message", {})
            data_b64 = message.get("data", "")
            if not data_b64:
                return None

            data_json = base64.b64decode(data_b64).decode("utf-8")
            data = json.loads(data_json)

            email_address = data.get("emailAddress")
            history_id = str(data.get("historyId", ""))

            if not email_address or not history_id:
                return None

            return cls(email_address=email_address, history_id=history_id)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, binascii.Error):
            logger.exception("Failed to parse Pub/Sub message")
            return None


@webhook_router.post("/webhook/gmail")
async def gmail_webhook(request: Request) -> Response:
    """Receive Gmail Pub/Sub push notifications.

    Google sends a POST when new messages arrive in a watched mailbox.
    We parse the notification, validate the coordinator exists, and
    enqueue background processing.

    Always returns 200 to acknowledge receipt (prevents Pub/Sub retries).
    """
    body = await request.json()

    pubsub_msg = PubSubMessage.from_request_body(body)
    if pubsub_msg is None:
        logger.warning("Received unparseable Pub/Sub message")
        return Response(status_code=200)

    logger.info(
        "Gmail push notification for %s, historyId=%s",
        pubsub_msg.email_address,
        pubsub_msg.history_id,
    )

    # Check if this coordinator has authorized the app
    token_store = getattr(request.app.state, "token_store", None)
    if token_store is None:
        logger.warning("TokenStore not initialized, skipping push notification")
        return Response(status_code=200)

    has_token = await token_store.has_token(pubsub_msg.email_address)
    if not has_token:
        logger.info("No token for %s, ignoring push", pubsub_msg.email_address)
        return Response(status_code=200)

    # Enqueue background processing via arq
    redis_pool = getattr(request.app.state, "redis", None)
    if redis_pool is not None:
        job = await redis_pool.enqueue_job(
            "process_gmail_notification",
            pubsub_msg.email_address,
            pubsub_msg.history_id,
        )
        if job is not None:
            logger.info("Enqueued job %s for %s", job.job_id, pubsub_msg.email_address)
        else:
            logger.warning(
                "Job already enqueued for %s (dedup)",
                pubsub_msg.email_address,
            )
    else:
        logger.warning("Redis not available, cannot enqueue push notification job")

    # Always return 200 to prevent Pub/Sub retries
    return Response(status_code=200)
