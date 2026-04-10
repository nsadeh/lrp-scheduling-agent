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
    CREATE INDEX idx_suggestions_coordinator_email
        ON agent_suggestions (coordinator_email);
    """,
    """
    DROP INDEX IF EXISTS idx_suggestions_coordinator_email;
    ALTER TABLE agent_suggestions DROP COLUMN coordinator_email;
    """,
)
