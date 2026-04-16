"""Google Workspace Add-on authentication.

Verifies the user's identity via the userIdToken in the request body.
Google signs this JWT — we verify it against Google's public certs to
confirm the user is who they claim to be.
"""

import logging

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)


async def verify_google_addon_token(request: Request) -> dict:
    """FastAPI dependency that verifies the user's identity from the add-on request.

    Extracts the userIdToken from the request body's authorizationEventObject,
    verifies it against Google's public certs, and returns the claims (including
    the user's email).
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid request body") from exc

    auth_obj = body.get("authorizationEventObject", {})
    user_id_token = auth_obj.get("userIdToken")

    if not user_id_token:
        raise HTTPException(
            status_code=401,
            detail="No userIdToken in request — cannot verify user identity",
        )

    try:
        claims = id_token.verify_token(
            user_id_token,
            request=google_requests.Request(),
        )
    except Exception as exc:
        logger.warning("userIdToken verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid user token") from exc

    email = claims.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="No email in user token")

    logger.info("Authenticated user: %s", email)
    return claims
