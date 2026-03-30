"""Sync-to-async bridge for the Google API Python client.

The googleapiclient library is synchronous (httplib2). We wrap calls in
asyncio.to_thread() to avoid blocking the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from googleapiclient.discovery import build

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


def _build_service(credentials: Credentials):
    """Build a Gmail API service object (synchronous)."""
    return build("gmail", "v1", credentials=credentials)


def _execute_sync(credentials: Credentials, fn) -> Any:
    """Build service and execute a callable that takes the service."""
    service = _build_service(credentials)
    return fn(service)


async def execute(credentials: Credentials, fn) -> Any:
    """Run a Gmail API call in a thread pool.

    Usage:
        result = await execute(creds, lambda svc: svc.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute())
    """
    return await asyncio.to_thread(_execute_sync, credentials, fn)
