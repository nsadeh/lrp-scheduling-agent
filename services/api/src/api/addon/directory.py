"""Google People API client for Workspace directory autocomplete.

Fetches the Workspace directory via ``people:listDirectoryPeople`` and
filters in Python to match the coordinator's query. Uses the calling
coordinator's own OAuth token (scope: ``directory.readonly``) — no
service account, no DWD.

Why list + client-side filter rather than ``searchDirectoryPeople``:
``searchDirectoryPeople`` does *prefix matching only* and indexes each
field independently. That means typing ``"adam@"`` prefix-matches literal
``"adam@"`` against full emails, which only works if the email begins
with exactly ``"adam@"`` (so ``adam@lrp.com`` works, but
``adam.smith@lrp.com`` does NOT). Typing a last name doesn't work
either, because names are indexed as first/last separately. For our
scale (~50 Workspace members per RFC §Scale context) a single list
call returns the whole directory in a few KB and filtering in Python
gives us substring matching, case insensitivity, and multi-token
queries ("sarah cheng") for ~free.
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

_LIST_URL = "https://people.googleapis.com/v1/people:listDirectoryPeople"
_READ_MASK = "names,emailAddresses,photos"
_SOURCE = "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"
# Safety cap on pagination — 3 pages of 1000 members = 3000 max. Well
# beyond LRP's ~50 projected directory size but stops a runaway loop
# if Google ever changes nextPageToken semantics.
_MAX_PAGES = 3
# Google enforces a max pageSize of 1000 on listDirectoryPeople.
_PAGE_SIZE = 1000


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


async def list_directory(creds: Credentials) -> list[DirectoryPerson]:
    """Fetch every member of the Workspace directory, all pages.

    One network call per page. For our ~50-member org this is almost
    always a single request. Caller filters the result in-process.
    """
    token = await _ensure_access_token(creds)
    headers = {"Authorization": f"Bearer {token}"}
    people: list[DirectoryPerson] = []
    page_token: str | None = None

    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {
                "readMask": _READ_MASK,
                "sources": _SOURCE,
                "pageSize": str(_PAGE_SIZE),
            }
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get(_LIST_URL, params=params, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "People API listDirectoryPeople returned %s: %s",
                    resp.status_code,
                    resp.text[:300],
                )
                resp.raise_for_status()

            data = resp.json()
            for raw in data.get("people", []):
                parsed = _parse_person(raw)
                if parsed is not None:
                    people.append(parsed)

            page_token = data.get("nextPageToken")
            if not page_token:
                break
        else:
            logger.warning(
                "list_directory: hit %d-page safety cap with nextPageToken still set",
                _MAX_PAGES,
            )

    return people


# Query text split on these characters (plus whitespace) into match-tokens.
# Lets "adam@" become ["adam"] and "sarah.cheng" become ["sarah", "cheng"].
_TOKEN_SEPARATORS = ("@", ".", ",")


def _tokenize_query(query: str) -> list[str]:
    normalized = query.lower()
    for sep in _TOKEN_SEPARATORS:
        normalized = normalized.replace(sep, " ")
    return [t for t in normalized.split() if t]


def _haystack(person: DirectoryPerson) -> str:
    return f"{person.display_name} {person.email}".lower()


def _matches(person: DirectoryPerson, tokens: list[str]) -> bool:
    """True when every token appears as a substring of the person's name or email."""
    hay = _haystack(person)
    return all(token in hay for token in tokens)


def _match_rank(person: DirectoryPerson, query_lower: str) -> tuple[int, str]:
    """Sort key: prefix matches first, then alphabetical by display name.

    Returns ``(rank, tiebreaker)`` — lower rank = better match.
    Rank 0: name OR email local-part starts with the raw query.
    Rank 1: any other substring match.
    """
    name_lower = person.display_name.lower()
    email_lower = person.email.lower()
    email_local = email_lower.split("@", 1)[0]
    if name_lower.startswith(query_lower) or email_local.startswith(query_lower):
        return (0, name_lower)
    return (1, name_lower)


async def search_directory(
    creds: Credentials,
    query: str,
    page_size: int = 10,
) -> list[DirectoryPerson]:
    """Return up to ``page_size`` directory members matching ``query``.

    Matching is case-insensitive substring: every token in the query
    (split on whitespace, ``@``, ``.``) must appear somewhere in the
    person's display name or email. Prefix matches rank above
    substring matches; ties break alphabetically by name.

    Returns an empty list for an empty query (avoids fetching the
    whole directory to show the user nothing).
    """
    if not query or not query.strip():
        return []

    tokens = _tokenize_query(query)
    if not tokens:
        return []

    everyone = await list_directory(creds)
    matches = [p for p in everyone if _matches(p, tokens)]
    query_lower = query.strip().lower()
    matches.sort(key=lambda p: _match_rank(p, query_lower))
    return matches[:page_size]
