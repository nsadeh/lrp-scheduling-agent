"""Clean up duplicate NEW loops created by runaway auto-creation.

The auto-resolver created thousands of duplicate loops (same candidate at
same client) because create_loop() had no dedup check. This migration:

1. For each (coordinator, candidate name, client) group with multiple
   state='new' loops, keeps the newest and marks the rest cold.
2. Marks all state='new' loops named "Unknown Candidate" as cold — these
   were created when the classifier failed to extract a name.

Cold is reversible via the existing revive() method; no data is deleted.
"""

from yoyo import step

# 1. Mark duplicate NEW loops as cold, keeping the newest per group.
step(
    """
    WITH ranked AS (
        SELECT
            l.id,
            ROW_NUMBER() OVER (
                PARTITION BY l.coordinator_id,
                             LOWER(TRIM(cand.name)),
                             l.client_contact_id
                ORDER BY l.created_at DESC
            ) AS rk
        FROM loops l
        JOIN candidates cand ON cand.id = l.candidate_id
        WHERE l.state = 'new'
    )
    UPDATE loops
    SET state = 'cold', updated_at = now()
    FROM ranked
    WHERE loops.id = ranked.id AND ranked.rk > 1;
    """,
    "SELECT 1;",
)

# 2. Mark all remaining NEW "Unknown Candidate" loops as cold.
step(
    """
    UPDATE loops
    SET state = 'cold', updated_at = now()
    WHERE state = 'new'
      AND candidate_id IN (
          SELECT id FROM candidates
          WHERE LOWER(TRIM(name)) = 'unknown candidate'
      );
    """,
    "SELECT 1;",
)
