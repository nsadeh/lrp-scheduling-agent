"""Fix un-normalised Gmail thread IDs stored before PR #72.

Two flavours leaked into the database:

  Type 1: thread-f:1861918515231589977        (decimal, needs hex)
  Type 2: thread-a:r-…|msg-f:1864470542700924161  (compound, extract msg-f → hex)

Affected tables: loop_email_threads, agent_suggestions, email_drafts.

loop_email_threads has UNIQUE(loop_id, gmail_thread_id), so we delete
colliding bad rows before updating the rest.
"""

from yoyo import step

# ── loop_email_threads ────────────────────────────────────────────────
# Delete bad rows whose corrected hex ID already exists for the same loop.

step(
    """
    DELETE FROM loop_email_threads bad
    USING loop_email_threads good
    WHERE bad.gmail_thread_id ~ '^thread-f:\\d+'
      AND good.loop_id = bad.loop_id
      AND good.gmail_thread_id = to_hex(
              (regexp_match(bad.gmail_thread_id, '^thread-f:(\\d+)'))[1]::bigint
          );
    """,
    "SELECT 1;",
)

step(
    """
    DELETE FROM loop_email_threads bad
    USING loop_email_threads good
    WHERE bad.gmail_thread_id LIKE '%|msg-f:%'
      AND good.loop_id = bad.loop_id
      AND good.gmail_thread_id = to_hex(
              (regexp_match(bad.gmail_thread_id, 'msg-f:(\\d+)'))[1]::bigint
          );
    """,
    "SELECT 1;",
)

# Update remaining bad rows (no collision).

step(
    """
    UPDATE loop_email_threads
    SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, '^thread-f:(\\d+)'))[1]::bigint)
    WHERE gmail_thread_id ~ '^thread-f:\\d+';
    """,
    "SELECT 1;",
)

step(
    """
    UPDATE loop_email_threads
    SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, 'msg-f:(\\d+)'))[1]::bigint)
    WHERE gmail_thread_id LIKE '%|msg-f:%';
    """,
    "SELECT 1;",
)

# ── agent_suggestions ────────────────────────────────────────────────

step(
    """
    UPDATE agent_suggestions
    SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, '^thread-f:(\\d+)'))[1]::bigint)
    WHERE gmail_thread_id ~ '^thread-f:\\d+';
    """,
    "SELECT 1;",
)

step(
    """
    UPDATE agent_suggestions
    SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, 'msg-f:(\\d+)'))[1]::bigint)
    WHERE gmail_thread_id LIKE '%|msg-f:%';
    """,
    "SELECT 1;",
)

# ── email_drafts ─────────────────────────────────────────────────────

step(
    """
    UPDATE email_drafts
    SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, '^thread-f:(\\d+)'))[1]::bigint)
    WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id ~ '^thread-f:\\d+';
    """,
    "SELECT 1;",
)

step(
    """
    UPDATE email_drafts
    SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, 'msg-f:(\\d+)'))[1]::bigint)
    WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id LIKE '%|msg-f:%';
    """,
    "SELECT 1;",
)
