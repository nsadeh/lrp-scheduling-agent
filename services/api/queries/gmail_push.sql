-- name: get_history_id(user_email)$
-- Load the last-processed Gmail history ID for incremental sync.
SELECT last_history_id FROM gmail_tokens WHERE user_email = :user_email;

-- name: update_history_id(user_email, last_history_id)!
-- Advance the history cursor after successful sync.
UPDATE gmail_tokens
SET last_history_id = :last_history_id, updated_at = now()
WHERE user_email = :user_email;

-- name: update_watch_state(user_email, last_history_id, watch_expiry)!
-- Update both history cursor and watch expiration after watch registration.
UPDATE gmail_tokens
SET last_history_id = :last_history_id,
    watch_expiry = :watch_expiry,
    updated_at = now()
WHERE user_email = :user_email;

-- name: get_all_watched_emails
-- List all coordinator emails with stored tokens (for poll fallback).
SELECT user_email FROM gmail_tokens;

-- name: is_message_processed(gmail_message_id)$
-- Check if a message has already been processed (dedup).
SELECT EXISTS(
    SELECT 1 FROM processed_messages WHERE gmail_message_id = :gmail_message_id
) AS is_processed;

-- name: mark_message_processed(gmail_message_id, coordinator_email)!
-- Record a message as processed. ON CONFLICT handles race between push and poll.
INSERT INTO processed_messages (gmail_message_id, coordinator_email)
VALUES (:gmail_message_id, :coordinator_email)
ON CONFLICT (gmail_message_id) DO NOTHING;

-- name: cleanup_old_processed_messages!
-- Delete dedup records older than 30 days.
DELETE FROM processed_messages WHERE processed_at < now() - INTERVAL '30 days';
