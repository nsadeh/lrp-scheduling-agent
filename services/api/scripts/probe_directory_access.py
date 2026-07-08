"""Empirical probe: can the coordinator's stored gmail.modify token call
Directory / People / Admin APIs to list workspace members?

Usage (from services/api):
    uv run python scripts/probe_directory_access.py nim@longridgepartners.com
"""

from __future__ import annotations

import asyncio
import sys

import httpx
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GAuthRequest
from psycopg_pool import AsyncConnectionPool

load_dotenv()

import os  # noqa: E402

from api.gmail.auth import TokenStore  # noqa: E402

PROBES = [
    (
        "People API: listDirectoryPeople (scope: directory.readonly)",
        "GET",
        "https://people.googleapis.com/v1/people:listDirectoryPeople"
        "?readMask=names,emailAddresses&sources=DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE&pageSize=5",
    ),
    (
        "People API: searchDirectoryPeople (scope: directory.readonly)",
        "GET",
        "https://people.googleapis.com/v1/people:searchDirectoryPeople"
        "?query=a&readMask=names,emailAddresses&sources=DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
    ),
    (
        "Admin SDK: users.list (scope: admin.directory.user.readonly)",
        "GET",
        "https://admin.googleapis.com/admin/directory/v1/users"
        "?domain=longridgepartners.com&maxResults=5",
    ),
    (
        "People API: otherContacts.list (scope: contacts.other.readonly)",
        "GET",
        "https://people.googleapis.com/v1/otherContacts"
        "?readMask=names,emailAddresses&pageSize=5",
    ),
    (
        "Drive API: files.list shared folder" " (scope: drive.readonly / drive.metadata.readonly)",
        "GET",
        "https://www.googleapis.com/drive/v3/files"
        "?q=mimeType%3D%27application%2Fvnd.google-apps.folder%27%20and%20sharedWithMe%3Dtrue"
        "&fields=files(id,name,owners)&pageSize=5",
    ),
    (
        "Drive API: files.list any file (scope: drive.file — app-created only)",
        "GET",
        "https://www.googleapis.com/drive/v3/files?pageSize=1&fields=files(id,name)",
    ),
    (
        "Gmail sanity-check: messages.list (scope: gmail.modify — should succeed)",
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=1",
    ),
]


async def main(user_email: str) -> None:
    db_url = os.environ["DATABASE_URL"]
    enc_key = os.environ["GMAIL_TOKEN_ENCRYPTION_KEY"]

    pool = AsyncConnectionPool(db_url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        store = TokenStore(pool, enc_key)
        creds = await store.load_credentials(user_email)
    finally:
        await pool.close()

    print(f"Stored scopes: {creds.scopes}\n")
    creds.refresh(GAuthRequest())
    print(f"Refreshed OK. Access token len={len(creds.token)}\n")

    async with httpx.AsyncClient(timeout=10) as client:
        for label, method, url in PROBES:
            r = await client.request(
                method, url, headers={"Authorization": f"Bearer {creds.token}"}
            )
            status = r.status_code
            body = r.text
            snippet = body[:300].replace("\n", " ")
            print(f"--- {label}")
            print(f"  {method} {url}")
            print(f"  status: {status}")
            print(f"  body:   {snippet}")
            print()


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else "nim@longridgepartners.com"
    asyncio.run(main(email))
