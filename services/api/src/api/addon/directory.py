"""Google People API client for Workspace directory autocomplete.

Wraps `people:searchDirectoryPeople` for the recruiter directory typeahead
in the create-loop form. Uses the calling coordinator's own OAuth token
(scope: directory.readonly) — no service account, no DWD.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx
from google.auth.transport.requests import Request as GAuthRequest

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://people.googleapis.com/v1/people:searchDirectoryPeople"
_READ_MASK = "names,emailAddresses,photos"
_SOURCE = "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"


class DirectoryPerson:
    """A single directory result, normalized from the People API response."""

    __slots__ = ("display_name", "email", "photo_url", "resource_name")

    def __init__(
        self,
        resource_name: str,
        display_name: str,
        email: str,
        photo_url: str | None,
    ):
        self.resource_name = resource_name
        self.display_name = display_name
        self.email = email
        self.photo_url = photo_url


async def _ensure_access_token(creds: Credentials) -> str:
    """Refresh the access token if needed and return it.

    google-auth's ``creds.refresh()`` issues a blocking HTTP call. Run it
    in a worker thread so we don't stall the asyncio event loop while a
    coordinator's ~hour-old access token is being refreshed.
    """
    if not creds.valid:
        await asyncio.to_thread(creds.refresh, GAuthRequest())
    return creds.token


def _parse_person(person: dict) -> DirectoryPerson | None:
    """Normalize one People API person into our slim DTO. Returns None if no email."""
    emails = person.get("emailAddresses") or []
    if not emails:
        return None
    email = emails[0].get("value")
    if not email:
        return None

    names = person.get("names") or []
    display_name = names[0].get("displayName", "") if names else ""

    photos = person.get("photos") or []
    photo_url = photos[0].get("url") if photos else None

    return DirectoryPerson(
        resource_name=person.get("resourceName", ""),
        display_name=display_name,
        email=email,
        photo_url=photo_url,
    )


async def search_directory(
    creds: Credentials,
    query: str,
    page_size: int = 10,
) -> list[DirectoryPerson]:
    """Call People API searchDirectoryPeople and return normalized results.

    Returns an empty list if the query is empty (Google rejects empty
    queries with 400 INVALID_ARGUMENT).
    """
    if not query:
        return []

    token = await _ensure_access_token(creds)
    params = {
        "query": query,
        "readMask": _READ_MASK,
        "sources": _SOURCE,
        "pageSize": str(page_size),
    }
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(_SEARCH_URL, params=params, headers=headers)

    if resp.status_code != 200:
        logger.warning(
            "People API searchDirectoryPeople returned %s: %s",
            resp.status_code,
            resp.text[:300],
        )
        resp.raise_for_status()

    data = resp.json()
    raw_people = [item.get("person", {}) for item in data.get("people", [])]
    parsed = [p for p in (_parse_person(rp) for rp in raw_people) if p is not None]
    return parsed
