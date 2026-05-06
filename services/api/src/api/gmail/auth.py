"""OAuth credential management with encrypted token storage in Postgres."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials

from api.gmail.exceptions import (
    GmailAuthError,
    GmailScopeError,
    GmailTokenStaleError,
    GmailUserNotAuthorizedError,
)
from api.gmail.queries import token_queries

if TYPE_CHECKING:
    from datetime import datetime

    from psycopg_pool import AsyncConnectionPool

# gmail.modify: read messages, send email on coordinator's behalf.
# directory.readonly: People API access for the recruiter directory
# autocomplete in the create-loop form (Workspace member typeahead).
_DEFAULT_SCOPES = ",".join(
    [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/directory.readonly",
    ]
)
SCOPES = os.environ.get("REQUIRED_SCOPES", _DEFAULT_SCOPES).split(",")
TOKEN_URI = "https://oauth2.googleapis.com/token"


class TokenStore:
    """Encrypted per-user OAuth refresh token storage in Postgres."""

    def __init__(self, db_pool: AsyncConnectionPool, encryption_key: str | bytes):
        if isinstance(encryption_key, str):
            encryption_key = encryption_key.encode()
        self._pool = db_pool
        self._fernet = Fernet(encryption_key)
        self._client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
        self._client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def _decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            raise GmailAuthError("Failed to decrypt token — wrong encryption key?") from exc

    async def store_token(self, user_email: str, refresh_token: str, scopes: list[str]) -> None:
        """Encrypt and upsert a refresh token for a user."""
        encrypted = self._encrypt(refresh_token)
        async with self._pool.connection() as conn:
            await token_queries.store_token(
                conn,
                user_email=user_email,
                refresh_token_encrypted=encrypted,
                scopes=scopes,
            )

    async def load_credentials(self, user_email: str) -> Credentials:
        """Load a user's stored token and return Google OAuth Credentials.

        Validates that stored scopes cover all REQUIRED_SCOPES. Raises
        GmailScopeError if re-authorization is needed.
        """
        async with self._pool.connection() as conn:
            row = await token_queries.load_token(conn, user_email=user_email)

        if row is None:
            raise GmailUserNotAuthorizedError(
                f"No stored token for {user_email}. User must authorize via the add-on first."
            )

        if row[2]:  # is_stale
            raise GmailTokenStaleError(
                f"Token for {user_email} is stale (revoked/expired). Re-authorization required."
            )

        granted = set(row[1])
        required = set(SCOPES)
        missing = required - granted
        if missing:
            raise GmailScopeError(
                f"User {user_email} is missing scopes: {missing}. Re-authorization required.",
                missing_scopes=list(missing),
            )

        refresh_token = self._decrypt(row[0])
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_uri=TOKEN_URI,
            scopes=row[1],
        )

    async def mark_stale(self, user_email: str) -> None:
        """Flag a token as stale after a RefreshError — stops further attempts."""
        async with self._pool.connection() as conn:
            await token_queries.mark_stale(conn, user_email=user_email)

    async def is_token_stale(self, user_email: str) -> bool:
        """Check if a user's token is marked stale."""
        async with self._pool.connection() as conn:
            result = await token_queries.is_token_stale(conn, user_email=user_email)
            return bool(result)

    async def delete_token(self, user_email: str) -> None:
        """Remove a user's stored token."""
        async with self._pool.connection() as conn:
            await token_queries.delete_token(conn, user_email=user_email)

    async def has_token(self, user_email: str) -> bool:
        """Check if a user has stored credentials."""
        async with self._pool.connection() as conn:
            return await token_queries.has_token(conn, user_email=user_email)

    # --- Push pipeline state ---

    async def get_history_id(self, user_email: str) -> str | None:
        """Load the last-processed Gmail history ID for incremental sync."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT last_history_id FROM gmail_tokens WHERE user_email = %(email)s",
                {"email": user_email},
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def update_history_id(self, user_email: str, history_id: str) -> None:
        """Advance the history cursor after successful sync."""
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE gmail_tokens
                SET last_history_id = %(history_id)s, updated_at = now()
                WHERE user_email = %(email)s
                """,
                {"email": user_email, "history_id": history_id},
            )

    async def update_watch_state(
        self, user_email: str, history_id: str, watch_expiry: datetime
    ) -> None:
        """Update both history cursor and watch expiration after watch registration."""
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE gmail_tokens
                SET last_history_id = %(history_id)s,
                    watch_expiry = %(watch_expiry)s,
                    updated_at = now()
                WHERE user_email = %(email)s
                """,
                {
                    "email": user_email,
                    "history_id": history_id,
                    "watch_expiry": watch_expiry,
                },
            )

    async def get_all_watched_emails(self) -> list[str]:
        """List all coordinator emails with valid (non-stale) stored tokens."""
        async with self._pool.connection() as conn:
            return [row[0] async for row in token_queries.get_all_watched_emails(conn)]
