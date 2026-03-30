#!/usr/bin/env python3
"""One-time OAuth bootstrap: authorize a user and store their refresh token.

Usage:
    uv run python scripts/gmail_oauth.py --user nim@kinematiclabs.dev

Prerequisites:
    - GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env or env
    - GMAIL_TOKEN_ENCRYPTION_KEY in .env or env
    - DATABASE_URL pointing to Postgres with gmail_tokens table
    - A GCP OAuth 2.0 "Desktop app" client configured for the project
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add src to path so we can import api.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from api.gmail.auth import SCOPES, TokenStore  # noqa: E402


def run_oauth_flow(client_id: str, client_secret: str) -> str:
    """Open browser for OAuth consent and return the refresh token."""
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
    creds = flow.run_local_server(port=8090, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("ERROR: No refresh token received. Try revoking app access and re-running.")
        sys.exit(1)

    return creds.refresh_token


async def store_token(user_email: str, refresh_token: str):
    """Store the refresh token in Postgres."""
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    encryption_key = os.environ["GMAIL_TOKEN_ENCRYPTION_KEY"]

    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()
    try:
        token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
        await token_store.store_token(user_email, refresh_token, SCOPES)
        print(f"Token stored for {user_email}")

        # Verify round-trip
        creds = await token_store.load_credentials(user_email)
        print(f"Verification: refresh_token loaded, client_id={creds.client_id[:20]}...")
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Gmail OAuth for a user")
    parser.add_argument("--user", required=True, help="Email address to authorize")
    args = parser.parse_args()

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("ERROR: Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET")
        sys.exit(1)

    if not os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY"):
        print("ERROR: Set GMAIL_TOKEN_ENCRYPTION_KEY")
        sys.exit(1)

    print(f"Starting OAuth flow for {args.user}")
    print("A browser window will open — sign in as the target user and grant access.")
    print()

    refresh_token = run_oauth_flow(client_id, client_secret)
    print(f"Got refresh token ({len(refresh_token)} chars)")

    asyncio.run(store_token(args.user, refresh_token))
    print("Done!")


if __name__ == "__main__":
    main()
