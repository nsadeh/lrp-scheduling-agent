"""Sync-to-async bridge for the Google API Python client.

The googleapiclient library is synchronous (httplib2). We wrap calls in
asyncio.to_thread() to avoid blocking the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import sentry_sdk
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

from api.gmail.exceptions import GmailTokenStaleError

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


def _build_service(credentials: Credentials):
    """Build a Gmail API service object (synchronous)."""
    return build("gmail", "v1", credentials=credentials)


def _execute_sync(credentials: Credentials, fn) -> Any:
    """Build service and execute a callable that takes the service."""
    service = _build_service(credentials)
    return fn(service)


async def execute(credentials: Credentials, fn, *, op_name: str = "gmail") -> Any:
    """Run a Gmail API call in a thread pool.

    Usage:
        result = await execute(creds, lambda svc: svc.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute())

    ``op_name`` is surfaced on the Sentry span so one transaction can show
    distinct messages.get vs messages.send vs history.list calls.
    """
    with sentry_sdk.start_span(op="http.client", name=f"gmail:{op_name}"):
        try:
            return await asyncio.to_thread(_execute_sync, credentials, fn)
        except RefreshError as exc:
            raise GmailTokenStaleError(f"Gmail token refresh failed: {exc}") from exc
