"""OAuth credential management with encrypted token storage in Postgres."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials

from api.gmail.exceptions import GmailAuthError, GmailUserNotAuthorizedError

if TYPE_CHECKING:
    from datetime import datetime

    from psycopg_pool import AsyncConnectionPool

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

_LOAD_SQL = "SELECT refresh_token_encrypted, scopes FROM gmail_tokens WHERE user_email = %(email)s"


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
            await conn.execute(
                """
                INSERT INTO gmail_tokens (user_email, refresh_token_encrypted, scopes, updated_at)
                VALUES (%(user_email)s, %(token)s, %(scopes)s, now())
                ON CONFLICT (user_email) DO UPDATE SET
                    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                    scopes = EXCLUDED.scopes,
                    updated_at = now()
                """,
                {"user_email": user_email, "token": encrypted, "scopes": scopes},
            )

    async def load_credentials(self, user_email: str) -> Credentials:
        """Load a user's stored token and return Google OAuth Credentials."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(_LOAD_SQL, {"email": user_email})
            row = await cur.fetchone()

        if row is None:
            raise GmailUserNotAuthorizedError(
                f"No stored token for {user_email}. User must authorize via the add-on first."
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

    async def delete_token(self, user_email: str) -> None:
        """Remove a user's stored token."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM gmail_tokens WHERE user_email = %(email)s",
                {"email": user_email},
            )

    async def has_token(self, user_email: str) -> bool:
        """Check if a user has stored credentials."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT EXISTS(SELECT 1 FROM gmail_tokens WHERE user_email = %(email)s)",
                {"email": user_email},
            )
            row = await cur.fetchone()
            return row[0] if row else False

    # --- Push notification state ---

    async def update_watch_state(
        self, user_email: str, history_id: str, watch_expiry: datetime
    ) -> None:
        """Update the last_history_id and watch_expiry for a coordinator."""
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

    async def get_history_id(self, user_email: str) -> str | None:
        """Get the last_history_id for a coordinator."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT last_history_id FROM gmail_tokens WHERE user_email = %(email)s",
                {"email": user_email},
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def update_history_id(self, user_email: str, history_id: str) -> None:
        """Advance the stored historyId after processing."""
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE gmail_tokens
                SET last_history_id = %(history_id)s,
                    updated_at = now()
                WHERE user_email = %(email)s
                """,
                {"email": user_email, "history_id": history_id},
            )

    async def get_watch_state(self, user_email: str) -> tuple[str | None, datetime | None]:
        """Get (last_history_id, watch_expiry) for a coordinator."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT last_history_id, watch_expiry FROM gmail_tokens"
                " WHERE user_email = %(email)s",
                {"email": user_email},
            )
            row = await cur.fetchone()
            if row is None:
                return (None, None)
            return (row[0], row[1])

    async def get_all_coordinators_with_tokens(self) -> list[str]:
        """Get all coordinator emails that have stored tokens (for watch renewal)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT user_email FROM gmail_tokens ORDER BY user_email")
            rows = await cur.fetchall()
            return [row[0] for row in rows]
