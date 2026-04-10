-- ============================================================
-- Processed messages (idempotent deduplication)
-- ============================================================

-- name: mark_message_processed!
-- Record that a message has been processed. Ignores duplicates.
INSERT INTO processed_messages (gmail_message_id, coordinator_email)
VALUES (:gmail_message_id, :coordinator_email)
ON CONFLICT (gmail_message_id) DO NOTHING;

-- name: is_message_processed$
-- Check if a message has already been processed.
SELECT EXISTS(
    SELECT 1 FROM processed_messages WHERE gmail_message_id = :gmail_message_id
) AS processed;

-- name: cleanup_old_processed_messages!
-- Remove processed message records older than 30 days.
DELETE FROM processed_messages
WHERE processed_at < now() - INTERVAL '30 days';

-- ============================================================
-- Agent suggestions
-- ============================================================

-- name: create_suggestion^
INSERT INTO agent_suggestions (
    id, loop_id, stage_id, gmail_message_id, gmail_thread_id,
    classification, suggested_action, questions, reasoning,
    confidence, prefilled_data, status, coordinator_email
)
VALUES (
    :id, :loop_id, :stage_id, :gmail_message_id, :gmail_thread_id,
    :classification, :suggested_action, :questions, :reasoning,
    :confidence, :prefilled_data, 'pending', :coordinator_email
)
RETURNING id, loop_id, stage_id, gmail_message_id, gmail_thread_id,
          classification, suggested_action, questions, reasoning,
          confidence, prefilled_data, status, coordinator_feedback,
          created_at, resolved_at, coordinator_email;

-- name: get_suggestion^
SELECT id, loop_id, stage_id, gmail_message_id, gmail_thread_id,
       classification, suggested_action, questions, reasoning,
       confidence, prefilled_data, status, coordinator_feedback,
       created_at, resolved_at, coordinator_email
FROM agent_suggestions
WHERE id = :id;

-- name: get_latest_suggestion_for_thread^
-- Most recent suggestion for a Gmail thread, scoped to a coordinator.
SELECT id, loop_id, stage_id, gmail_message_id, gmail_thread_id,
       classification, suggested_action, questions, reasoning,
       confidence, prefilled_data, status, coordinator_feedback,
       created_at, resolved_at, coordinator_email
FROM agent_suggestions
WHERE gmail_thread_id = :gmail_thread_id
  AND coordinator_email = :coordinator_email
ORDER BY created_at DESC
LIMIT 1;

-- name: get_latest_suggestion_for_loop^
-- Most recent suggestion for a loop.
SELECT id, loop_id, stage_id, gmail_message_id, gmail_thread_id,
       classification, suggested_action, questions, reasoning,
       confidence, prefilled_data, status, coordinator_feedback,
       created_at, resolved_at, coordinator_email
FROM agent_suggestions
WHERE loop_id = :loop_id
ORDER BY created_at DESC
LIMIT 1;

-- name: get_pending_suggestions_for_coordinator
-- All pending suggestions for a coordinator.
SELECT id, loop_id, stage_id, gmail_message_id, gmail_thread_id,
       classification, suggested_action, questions, reasoning,
       confidence, prefilled_data, status, coordinator_feedback,
       created_at, resolved_at, coordinator_email
FROM agent_suggestions
WHERE status = 'pending'
  AND coordinator_email = :coordinator_email
ORDER BY created_at DESC;

-- name: resolve_suggestion!
-- Mark a suggestion as accepted/edited/rejected.
UPDATE agent_suggestions
SET status = :status,
    coordinator_feedback = :coordinator_feedback,
    resolved_at = now()
WHERE id = :id;

-- ============================================================
-- Suggestion drafts
-- ============================================================

-- name: create_suggestion_draft^
INSERT INTO suggestion_drafts (id, suggestion_id, draft_to, draft_subject, draft_body, in_reply_to)
VALUES (:id, :suggestion_id, :draft_to, :draft_subject, :draft_body, :in_reply_to)
RETURNING id, suggestion_id, draft_to, draft_subject, draft_body, in_reply_to, created_at;

-- name: get_draft_for_suggestion^
SELECT id, suggestion_id, draft_to, draft_subject, draft_body, in_reply_to, created_at
FROM suggestion_drafts
WHERE suggestion_id = :suggestion_id;

-- ============================================================
-- Gmail watch state (stored in gmail_tokens)
-- ============================================================

-- name: update_watch_state!
UPDATE gmail_tokens
SET last_history_id = :history_id,
    watch_expiry = :watch_expiry,
    updated_at = now()
WHERE user_email = :user_email;

-- name: get_watch_state^
SELECT last_history_id, watch_expiry
FROM gmail_tokens
WHERE user_email = :user_email;

-- name: update_history_id!
UPDATE gmail_tokens
SET last_history_id = :history_id,
    updated_at = now()
WHERE user_email = :user_email;

-- name: get_all_coordinator_emails
-- All coordinators who have authorized Gmail access.
SELECT user_email
FROM gmail_tokens;

-- ============================================================
-- Contact lookups for pre-filter
-- ============================================================

-- name: has_known_contact$
-- Check if an email belongs to any known contact (recruiter, client, CM).
SELECT EXISTS(
    SELECT 1 FROM contacts WHERE email = :email
    UNION ALL
    SELECT 1 FROM client_contacts WHERE email = :email
) AS known;

-- name: find_client_contact_by_email^
SELECT id, name, email, company, created_at
FROM client_contacts
WHERE email = :email;

-- name: find_contact_by_email^
SELECT id, name, email, role, company, created_at
FROM contacts
WHERE email = :email;
