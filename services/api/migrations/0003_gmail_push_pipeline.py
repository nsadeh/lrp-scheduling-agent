"""Add Gmail push notification pipeline tables.

- processed_messages: idempotent deduplication for push + pull sync
- gmail_tokens: add last_history_id and watch_expiry columns
"""

from yoyo import step

step(
    """
    CREATE TABLE processed_messages (
        gmail_message_id    TEXT PRIMARY KEY,
        coordinator_email   TEXT NOT NULL,
        processed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    ALTER TABLE gmail_tokens
        ADD COLUMN last_history_id TEXT,
        ADD COLUMN watch_expiry TIMESTAMPTZ;
    """,
    """
    ALTER TABLE gmail_tokens
        DROP COLUMN IF EXISTS watch_expiry,
        DROP COLUMN IF EXISTS last_history_id;

    DROP TABLE IF EXISTS processed_messages;
    """,
)
