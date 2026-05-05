"""Collapse stages into a single loop.state column.

Stages were a separate entity to support multi-round interviews (R1, R2,
Final), but in practice multi-round loops are barely used and the new
agent architecture doesn't have a clean way to start a follow-up round
on a thread already linked to a complete loop. Collapsing means: each
loop has ONE state at any time, computed as the most-actionable of its
former stages (lowest priority number wins).

Also drops legacy agent_suggestions columns: target_state, auto_advance,
extracted_entities, questions. These were redundant after action_data
JSONB shipped — the new prompt collapses them all into action_data.

Destructive: stages, time_slots, and the dropped suggestion columns are
unrecoverable on rollback. User explicitly accepted this.
"""

from yoyo import step

# 1. Add the new state column on loops with a temporary default for the backfill.
step(
    """
    ALTER TABLE loops
    ADD COLUMN state TEXT NOT NULL DEFAULT 'new';
    """,
    "ALTER TABLE loops DROP COLUMN state;",
)

# 2. Backfill: state = the most-actionable stage's state per loop.
#    Priority order mirrors STATE_PRIORITY in scheduling/models.py — lower number
#    means more urgent (closer to needing coordinator action). Tie between
#    awaiting_candidate and awaiting_client breaks toward awaiting_candidate
#    so the onus stays internal (with the recruiter).
#    Loops with no stages stay at the default 'new'.
step(
    """
    WITH ranked_stages AS (
        SELECT
            loop_id,
            state,
            ROW_NUMBER() OVER (
                PARTITION BY loop_id
                ORDER BY
                    CASE state
                        WHEN 'new' THEN 0
                        WHEN 'awaiting_candidate' THEN 1
                        WHEN 'awaiting_client' THEN 2
                        WHEN 'scheduled' THEN 3
                        WHEN 'complete' THEN 4
                        WHEN 'cold' THEN 5
                    END
            ) AS rk
        FROM stages
    )
    UPDATE loops l
    SET state = rs.state
    FROM ranked_stages rs
    WHERE rs.loop_id = l.id AND rs.rk = 1;
    """,
    "SELECT 1;",
)

# 3a. Migrate ADVANCE_STAGE rows: target_state -> action_data.target_stage
step(
    """
    UPDATE agent_suggestions
    SET action_data = jsonb_set(
        COALESCE(action_data, '{}'::jsonb),
        '{target_stage}',
        to_jsonb(target_state)
    )
    WHERE action = 'advance_stage'
      AND target_state IS NOT NULL
      AND NOT (action_data ? 'target_stage');
    """,
    "SELECT 1;",
)

# 3b. Migrate ASK_COORDINATOR rows: questions[0] -> action_data.question
step(
    """
    UPDATE agent_suggestions
    SET action_data = jsonb_set(
        COALESCE(action_data, '{}'::jsonb),
        '{question}',
        questions->0
    )
    WHERE action = 'ask_coordinator'
      AND jsonb_array_length(COALESCE(questions, '[]'::jsonb)) > 0
      AND NOT (action_data ? 'question');
    """,
    "SELECT 1;",
)

# 3c. Convert MARK_COLD rows to ADVANCE_STAGE with target_stage='cold'.
#     The MARK_COLD enum value is being removed in this same change, so any
#     rows we leave with action='mark_cold' would fail Pydantic validation
#     when re-read.
step(
    """
    UPDATE agent_suggestions
    SET action = 'advance_stage',
        action_data = jsonb_set(
            COALESCE(action_data, '{}'::jsonb),
            '{target_stage}',
            '"cold"'::jsonb
        )
    WHERE action = 'mark_cold';
    """,
    "SELECT 1;",
)

# 4. Drop FK columns to stages from agent_suggestions, email_drafts, loop_events.
step(
    """
    ALTER TABLE agent_suggestions DROP COLUMN stage_id;
    ALTER TABLE email_drafts       DROP COLUMN stage_id;
    ALTER TABLE loop_events        DROP COLUMN stage_id;
    DROP INDEX IF EXISTS idx_loop_events_stage;
    """,
    """
    ALTER TABLE agent_suggestions ADD COLUMN stage_id TEXT;
    ALTER TABLE email_drafts       ADD COLUMN stage_id TEXT;
    ALTER TABLE loop_events        ADD COLUMN stage_id TEXT;
    """,
)

# 5. Drop the now-redundant legacy suggestion columns.
step(
    """
    ALTER TABLE agent_suggestions
        DROP COLUMN target_state,
        DROP COLUMN auto_advance,
        DROP COLUMN extracted_entities,
        DROP COLUMN questions;
    """,
    """
    ALTER TABLE agent_suggestions
        ADD COLUMN target_state TEXT,
        ADD COLUMN auto_advance BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN extracted_entities JSONB NOT NULL DEFAULT '{}',
        ADD COLUMN questions JSONB NOT NULL DEFAULT '[]';
    """,
)

# 6. Drop time_slots and stages. time_slots was bound to stages and never
#    surfaced in any user-facing path (no addon UI, no LLM context, no draft
#    consumer) — drop it entirely. CASCADE handles any remaining FK refs.
step(
    """
    DROP TABLE IF EXISTS time_slots CASCADE;
    DROP TABLE IF EXISTS stages CASCADE;
    """,
    # No down — recreating the tables empty doesn't restore the data.
    "SELECT 1;",
)
