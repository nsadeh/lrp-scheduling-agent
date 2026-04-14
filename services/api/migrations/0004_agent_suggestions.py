"""Create agent_suggestions table for classifier output persistence."""

from yoyo import step

step(
    """
    CREATE TABLE agent_suggestions (
        id                  TEXT PRIMARY KEY,
        coordinator_email   TEXT NOT NULL,
        gmail_message_id    TEXT NOT NULL,
        gmail_thread_id     TEXT NOT NULL,
        loop_id             TEXT REFERENCES loops(id),
        stage_id            TEXT REFERENCES stages(id),
        classification      TEXT NOT NULL,
        action              TEXT NOT NULL,
        auto_advance        BOOLEAN NOT NULL DEFAULT false,
        confidence          REAL NOT NULL,
        summary             TEXT NOT NULL,
        target_state        TEXT,
        extracted_entities  JSONB NOT NULL DEFAULT '{}',
        questions           JSONB NOT NULL DEFAULT '[]',
        reasoning           TEXT,
        status              TEXT NOT NULL DEFAULT 'pending',
        resolved_at         TIMESTAMPTZ,
        resolved_by         TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX idx_suggestions_coordinator_status
        ON agent_suggestions(coordinator_email, status);
    CREATE INDEX idx_suggestions_thread
        ON agent_suggestions(gmail_thread_id);
    CREATE INDEX idx_suggestions_loop
        ON agent_suggestions(loop_id)
        WHERE loop_id IS NOT NULL;
    """,
    """
    DROP TABLE agent_suggestions;
    """,
)
