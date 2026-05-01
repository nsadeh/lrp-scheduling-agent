-- name: get_history_id(user_email)$
-- Load the last-processed Gmail history ID for incremental sync.
SELECT last_history_id FROM gmail_tokens WHERE user_email = :user_email;

-- name: update_history_id(user_email, last_history_id)!
-- Advance the history cursor after successful sync.
UPDATE gmail_tokens
SET last_history_id = :last_history_id, updated_at = now()
WHERE user_email = :user_email;

-- name: update_watch_state(user_email, last_history_id, watch_expiry)!
-- Update watch expiration and advance history cursor (never backward).
UPDATE gmail_tokens
SET last_history_id = GREATEST(last_history_id, :last_history_id),
    watch_expiry = :watch_expiry,
    updated_at = now()
WHERE user_email = :user_email;

-- name: get_all_watched_emails
-- List coordinator emails with active watches (for poll fallback and watch renewal).
SELECT user_email FROM gmail_tokens
WHERE watch_expiry IS NOT NULL AND watch_expiry > now();

-- name: get_processed_message_ids
-- Of the given message IDs, return the ones already in processed_messages.
-- Used by the push worker to filter a batch of incoming messages down to the
-- ones we haven't classified yet — replaces a per-message EXISTS check.
SELECT gmail_message_id
FROM processed_messages
WHERE gmail_message_id = ANY(:message_ids);

-- name: mark_messages_processed_batch!
-- Bulk-record the given message IDs as processed for one coordinator.
-- ON CONFLICT handles races between push and poll workers seeing the same
-- message; SELECT ... unnest lets us insert N rows in one round-trip.
INSERT INTO processed_messages (gmail_message_id, coordinator_email)
SELECT unnest(:message_ids::TEXT[]), :coordinator_email
ON CONFLICT (gmail_message_id) DO NOTHING;

-- name: cleanup_old_processed_messages!
-- Delete dedup records older than 30 days.
DELETE FROM processed_messages WHERE processed_at < now() - INTERVAL '30 days';
