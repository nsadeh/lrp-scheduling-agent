"""Add is_stale flag to gmail_tokens for marking revoked/expired tokens."""

from yoyo import step

step(
    """
    ALTER TABLE gmail_tokens
        ADD COLUMN is_stale BOOLEAN NOT NULL DEFAULT false;
    """,
    """
    ALTER TABLE gmail_tokens
        DROP COLUMN is_stale;
    """,
)
