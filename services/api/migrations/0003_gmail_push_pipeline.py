"""Add push pipeline state to gmail_tokens and create processed_messages dedup table."""

from yoyo import step

step(
    """
    ALTER TABLE gmail_tokens
        ADD COLUMN last_history_id TEXT,
        ADD COLUMN watch_expiry TIMESTAMPTZ;
    """,
    """
    ALTER TABLE gmail_tokens
        DROP COLUMN last_history_id,
        DROP COLUMN watch_expiry;
    """,
)

step(
    """
    CREATE TABLE processed_messages (
        gmail_message_id    TEXT PRIMARY KEY,
        coordinator_email   TEXT NOT NULL,
        processed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX idx_processed_messages_cleanup
        ON processed_messages (processed_at);
    """,
    """
    DROP TABLE processed_messages;
    """,
)
