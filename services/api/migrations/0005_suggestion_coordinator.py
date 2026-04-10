"""Add coordinator_email to agent_suggestions for data isolation.

Without this column, suggestions are looked up by thread_id alone,
meaning any coordinator viewing a thread could see suggestions meant
for a different coordinator.
"""

from yoyo import step

step(
    """
    ALTER TABLE agent_suggestions
        ADD COLUMN coordinator_email TEXT NOT NULL DEFAULT '';
    """,
    """
    ALTER TABLE agent_suggestions DROP COLUMN coordinator_email;
    """,
)

# Backfill existing rows: assign them to the coordinator who processed the
# message (matched via processed_messages). Rows with no match remain '' and
# will be cleaned up in the next step.
step(
    """
    UPDATE agent_suggestions s
    SET coordinator_email = pm.coordinator_email
    FROM processed_messages pm
    WHERE pm.gmail_message_id = s.gmail_message_id
      AND s.coordinator_email = '';
    """,
    "",  # rollback is a no-op — the column is dropped by the previous step's rollback
)

# Delete any orphaned rows that couldn't be backfilled (no matching processed_messages).
# These suggestions are invisible to all coordinators anyway.
step(
    """
    DELETE FROM agent_suggestions WHERE coordinator_email = '';
    """,
    "",
)

step(
    """
    CREATE INDEX idx_suggestions_coordinator_email
        ON agent_suggestions (coordinator_email);
    """,
    """
    DROP INDEX IF EXISTS idx_suggestions_coordinator_email;
    """,
)
