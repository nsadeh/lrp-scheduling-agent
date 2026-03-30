"""Create gmail_tokens table for storing encrypted per-user OAuth refresh tokens."""

from yoyo import step

step(
    """
    CREATE TABLE gmail_tokens (
        user_email              TEXT PRIMARY KEY,
        refresh_token_encrypted BYTEA NOT NULL,
        scopes                  TEXT[] NOT NULL,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    DROP TABLE gmail_tokens;
    """,
)
