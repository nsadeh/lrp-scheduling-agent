"""Create scheduled_interviews table — booked interviews tracked per loop."""

from yoyo import step

step(
    """
    CREATE TABLE scheduled_interviews (
        id                      TEXT PRIMARY KEY,
        loop_id                 TEXT NOT NULL REFERENCES loops(id),
        interview_start_time    TIMESTAMPTZ NOT NULL,
        interview_duration      INTEGER NOT NULL,
        interview_notes         TEXT NOT NULL DEFAULT '',
        is_canceled             BOOLEAN NOT NULL DEFAULT false,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_scheduled_interviews_loop ON scheduled_interviews(loop_id);
    """,
    """
    DROP TABLE IF EXISTS scheduled_interviews;
    """,
)
