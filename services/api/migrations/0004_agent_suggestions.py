"""Add agent suggestion tables.

agent_suggestions: persisted agent output (classification + action + confidence)
suggestion_drafts: optional email drafts attached to suggestions
"""

from yoyo import step

step(
    """
    CREATE TABLE agent_suggestions (
        id                  TEXT PRIMARY KEY,
        loop_id             TEXT REFERENCES loops(id),
        stage_id            TEXT REFERENCES stages(id),
        gmail_message_id    TEXT NOT NULL,
        gmail_thread_id     TEXT NOT NULL,
        classification      TEXT NOT NULL,
        suggested_action    TEXT NOT NULL,
        questions           TEXT[],
        reasoning           TEXT,
        confidence          REAL NOT NULL,
        prefilled_data      JSONB,
        status              TEXT NOT NULL DEFAULT 'pending',
        coordinator_feedback TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolved_at         TIMESTAMPTZ
    );

    CREATE TABLE suggestion_drafts (
        id                  TEXT PRIMARY KEY,
        suggestion_id       TEXT NOT NULL REFERENCES agent_suggestions(id),
        draft_to            TEXT[] NOT NULL,
        draft_subject       TEXT NOT NULL,
        draft_body          TEXT NOT NULL,
        in_reply_to         TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX idx_suggestions_loop ON agent_suggestions(loop_id, created_at DESC);
    CREATE INDEX idx_suggestions_thread ON agent_suggestions(gmail_thread_id, created_at DESC);
    CREATE INDEX idx_suggestions_pending ON agent_suggestions(status) WHERE status = 'pending';
    CREATE INDEX idx_drafts_suggestion ON suggestion_drafts(suggestion_id);
    """,
    """
    DROP TABLE IF EXISTS suggestion_drafts;
    DROP TABLE IF EXISTS agent_suggestions;
    """,
)
