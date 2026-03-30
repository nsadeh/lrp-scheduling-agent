"""Google Workspace Add-on token verification.

Google sends an Authorization: Bearer <id_token> header with every request to
our add-on endpoints. We verify this token to confirm the request originates
from Google and targets our GCP project.

The google.auth.transport.requests.Request() transport is synchronous (urllib3),
but Google's public certs are cached for ~6 hours so the blocking call is rare
and fast. Acceptable for our scale.
"""

import logging
import os

from fastapi import Header, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

GCP_PROJECT_NUMBER = os.environ.get("GCP_PROJECT_NUMBER", "")
GOOGLE_ADDON_SA_EMAIL = os.environ.get("GOOGLE_ADDON_SERVICE_ACCOUNT_EMAIL", "")
SKIP_ADDON_AUTH = os.environ.get("SKIP_ADDON_AUTH", "").lower() == "true"

if SKIP_ADDON_AUTH:
    logger.warning(
        "SKIP_ADDON_AUTH is enabled — add-on token verification is DISABLED. "
        "This must NEVER be set in production."
    )


async def verify_google_addon_token(
    request: Request,
    authorization: str = Header(default=""),
) -> dict:
    """FastAPI dependency that verifies the Google-issued ID token.

    For HTTP-based Workspace add-ons, Google sets the token audience to the
    endpoint URL being called (not the GCP project number). We verify against
    the request URL.

    Returns the verified token claims dict. In skip mode, returns a stub.
    """
    if SKIP_ADDON_AUTH:
        return {"iss": "skip", "email": "skip@test"}

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ")

    # Google sets the audience to the endpoint URL for HTTP add-ons
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

    logger.info("Token claims: iss=%s, email=%s", claims.get("iss"), claims.get("email"))

    valid_issuers = {"accounts.google.com", "https://accounts.google.com"}
    if claims.get("iss") not in valid_issuers:
        logger.warning("Invalid issuer: %s", claims.get("iss"))
        raise HTTPException(status_code=401, detail="Invalid token issuer")

    if GOOGLE_ADDON_SA_EMAIL and claims.get("email") != GOOGLE_ADDON_SA_EMAIL:
        raise HTTPException(status_code=401, detail="Unauthorized caller")

    return claims
