"""Empirical probe for the `directory.readonly` path.

Runs a one-off OAuth flow using the backend's existing OAuth client,
requesting gmail.modify + directory.readonly. Opens a browser for consent,
captures the token, then exercises People API endpoints to confirm:
  (1) searchDirectoryPeople works
  (2) listDirectoryPeople works
  (3) what fields actually come back (name / email / avatar / other)

Usage (from services/api):
    uv run python scripts/probe_directory_scope.py

Does NOT touch the gmail_tokens table. The token produced here is ephemeral
and only used for this probe.
"""

from __future__ import annotations

import json
import os

import httpx
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/directory.readonly",
]


def run_oauth() -> str:
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message=(
            "Consent screen opened in your browser."
            " Approve the 'See and download your organization's Directory' line."
        ),
    )
    return creds.token


def probe(token: str) -> None:
    with httpx.Client(timeout=10) as client:
        hdr = {"Authorization": f"Bearer {token}"}

        print("\n=== searchDirectoryPeople (typeahead) ===")
        r = client.get(
            "https://people.googleapis.com/v1/people:searchDirectoryPeople",
            params={
                "query": "a",
                "readMask": "names,emailAddresses,photos,organizations",
                "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                "pageSize": 5,
            },
            headers=hdr,
        )
        print(f"status: {r.status_code}")
        print(json.dumps(r.json(), indent=2)[:1500])

        print("\n=== listDirectoryPeople (full dump, first page) ===")
        r = client.get(
            "https://people.googleapis.com/v1/people:listDirectoryPeople",
            params={
                "readMask": "names,emailAddresses,photos,organizations,metadata",
                "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                "pageSize": 10,
            },
            headers=hdr,
        )
        print(f"status: {r.status_code}")
        payload = r.json()
        # Keep output compact: show count + one full sample record
        people = payload.get("people", [])
        print(f"returned {len(people)} people; nextPageToken={payload.get('nextPageToken')}")
        if people:
            print("\nSample record (first):")
            print(json.dumps(people[0], indent=2))


if __name__ == "__main__":
    print("Starting OAuth flow — a browser will open.")
    token = run_oauth()
    print(f"Got access token (len={len(token)}).")
    probe(token)
