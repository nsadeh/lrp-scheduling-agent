-- fix_bad_thread_ids.sql
--
-- Cleans up two flavours of un-normalised Gmail thread IDs that leaked
-- into the database before / despite the _normalize_gmail_id() fix (PR #72):
--
--   Type 1: thread-f:1861918515231589977        (decimal, needs hex)
--   Type 2: thread-a:r-…|msg-f:1864470542700924161  (compound, extract msg-f decimal → hex)
--
-- Affected tables: loop_email_threads, agent_suggestions, email_drafts
--
-- Usage:
--   1. Run the dry-run SELECTs first to inspect what will change.
--   2. Run the full script inside a transaction (BEGIN … COMMIT).
--   3. Run the verification query at the bottom.

BEGIN;

-- ============================================================
-- DRY RUN — preview what will change
-- ============================================================

-- Type 1: bare thread-f:DIGITS
SELECT 'loop_email_threads' AS tbl, id, gmail_thread_id AS old_id,
       to_hex((regexp_match(gmail_thread_id, '^thread-f:(\d+)'))[1]::bigint) AS new_id
FROM loop_email_threads
WHERE gmail_thread_id ~ '^thread-f:\d+'

UNION ALL

SELECT 'agent_suggestions', id, gmail_thread_id,
       to_hex((regexp_match(gmail_thread_id, '^thread-f:(\d+)'))[1]::bigint)
FROM agent_suggestions
WHERE gmail_thread_id ~ '^thread-f:\d+'

UNION ALL

SELECT 'email_drafts', id, gmail_thread_id,
       to_hex((regexp_match(gmail_thread_id, '^thread-f:(\d+)'))[1]::bigint)
FROM email_drafts
WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id ~ '^thread-f:\d+';

-- Type 2: compound …|msg-f:DIGITS
SELECT 'loop_email_threads' AS tbl, id, gmail_thread_id AS old_id,
       to_hex((regexp_match(gmail_thread_id, 'msg-f:(\d+)'))[1]::bigint) AS new_id
FROM loop_email_threads
WHERE gmail_thread_id LIKE '%|msg-f:%'

UNION ALL

SELECT 'agent_suggestions', id, gmail_thread_id,
       to_hex((regexp_match(gmail_thread_id, 'msg-f:(\d+)'))[1]::bigint)
FROM agent_suggestions
WHERE gmail_thread_id LIKE '%|msg-f:%'

UNION ALL

SELECT 'email_drafts', id, gmail_thread_id,
       to_hex((regexp_match(gmail_thread_id, 'msg-f:(\d+)'))[1]::bigint)
FROM email_drafts
WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id LIKE '%|msg-f:%';


-- ============================================================
-- FIX: loop_email_threads (has UNIQUE(loop_id, gmail_thread_id))
-- ============================================================

-- Step 1: delete bad rows whose corrected ID would collide with an
-- existing good row for the same loop (the good row wins).

-- Type 1 duplicates
DELETE FROM loop_email_threads bad
USING loop_email_threads good
WHERE bad.gmail_thread_id ~ '^thread-f:\d+'
  AND good.loop_id = bad.loop_id
  AND good.gmail_thread_id = to_hex((regexp_match(bad.gmail_thread_id, '^thread-f:(\d+)'))[1]::bigint);

-- Type 2 duplicates
DELETE FROM loop_email_threads bad
USING loop_email_threads good
WHERE bad.gmail_thread_id LIKE '%|msg-f:%'
  AND good.loop_id = bad.loop_id
  AND good.gmail_thread_id = to_hex((regexp_match(bad.gmail_thread_id, 'msg-f:(\d+)'))[1]::bigint);

-- Step 2: update remaining bad rows (no collision).
UPDATE loop_email_threads
SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, '^thread-f:(\d+)'))[1]::bigint)
WHERE gmail_thread_id ~ '^thread-f:\d+';

UPDATE loop_email_threads
SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, 'msg-f:(\d+)'))[1]::bigint)
WHERE gmail_thread_id LIKE '%|msg-f:%';


-- ============================================================
-- FIX: agent_suggestions (no unique constraint on gmail_thread_id)
-- ============================================================

UPDATE agent_suggestions
SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, '^thread-f:(\d+)'))[1]::bigint)
WHERE gmail_thread_id ~ '^thread-f:\d+';

UPDATE agent_suggestions
SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, 'msg-f:(\d+)'))[1]::bigint)
WHERE gmail_thread_id LIKE '%|msg-f:%';


-- ============================================================
-- FIX: email_drafts (gmail_thread_id is nullable, no unique constraint)
-- ============================================================

UPDATE email_drafts
SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, '^thread-f:(\d+)'))[1]::bigint)
WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id ~ '^thread-f:\d+';

UPDATE email_drafts
SET gmail_thread_id = to_hex((regexp_match(gmail_thread_id, 'msg-f:(\d+)'))[1]::bigint)
WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id LIKE '%|msg-f:%';


-- ============================================================
-- VERIFY — no bad IDs remain
-- ============================================================

SELECT 'loop_email_threads' AS tbl, count(*) AS remaining_bad
FROM loop_email_threads
WHERE gmail_thread_id LIKE '%|%' OR gmail_thread_id ~ '^thread-[a-z]:'

UNION ALL

SELECT 'agent_suggestions', count(*)
FROM agent_suggestions
WHERE gmail_thread_id LIKE '%|%' OR gmail_thread_id ~ '^thread-[a-z]:'

UNION ALL

SELECT 'email_drafts', count(*)
FROM email_drafts
WHERE gmail_thread_id IS NOT NULL
  AND (gmail_thread_id LIKE '%|%' OR gmail_thread_id ~ '^thread-[a-z]:');

COMMIT;
