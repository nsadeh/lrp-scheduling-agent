"""Google Workspace Add-on request verification.

Verifies that requests to add-on endpoints originate from Google by
checking the systemIdToken in the Authorization header. This is a
Google-signed JWT whose audience matches the request URL.

The user's identity (email) is NOT extracted here — it comes from the
userIdToken in the request body, handled by _get_user_email() in routes.py.
"""

import logging

from fastapi import Header, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)


async def verify_google_addon_token(
    request: Request,
    authorization: str = Header(default=""),
) -> dict:
    """FastAPI dependency that verifies the request came from Google.

    Google sends a systemIdToken as a Bearer token in the Authorization header.
    We verify the JWT signature and audience (must match the request URL).

    Returns the verified token claims. The user's email is NOT in these claims —
    use _get_user_email() to extract it from the body's userIdToken.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ")
    expected_audience = str(request.url)

    try:
        claims = id_token.verify_token(
            token,
            request=google_requests.Request(),
            audience=expected_audience,
        )
    except Exception as exc:
        logger.warning("Token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    logger.debug("Verified add-on request from Google")
    return claims
