-- ============================================================
-- Email Drafts
-- ============================================================

-- name: create_draft^
INSERT INTO email_drafts (
    id, suggestion_id, loop_id, stage_id, coordinator_email,
    to_emails, cc_emails, subject, body, gmail_thread_id, status
)
VALUES (
    :id, :suggestion_id, :loop_id, :stage_id, :coordinator_email,
    :to_emails, :cc_emails, :subject, :body, :gmail_thread_id, :status
)
RETURNING id, suggestion_id, loop_id, stage_id, coordinator_email,
          to_emails, cc_emails, subject, body, gmail_thread_id,
          status, sent_at, created_at, updated_at;

-- name: get_draft^
SELECT id, suggestion_id, loop_id, stage_id, coordinator_email,
       to_emails, cc_emails, subject, body, gmail_thread_id,
       status, sent_at, created_at, updated_at
FROM email_drafts
WHERE id = :id;

-- name: get_draft_for_suggestion^
SELECT id, suggestion_id, loop_id, stage_id, coordinator_email,
       to_emails, cc_emails, subject, body, gmail_thread_id,
       status, sent_at, created_at, updated_at
FROM email_drafts
WHERE suggestion_id = :suggestion_id;

-- name: get_pending_drafts_for_coordinator
SELECT id, suggestion_id, loop_id, stage_id, coordinator_email,
       to_emails, cc_emails, subject, body, gmail_thread_id,
       status, sent_at, created_at, updated_at
FROM email_drafts
WHERE coordinator_email = :coordinator_email
  AND status IN ('generated', 'edited')
ORDER BY created_at DESC;

-- name: update_draft_body!
UPDATE email_drafts
SET body = :body, status = 'edited', updated_at = now()
WHERE id = :id;

-- name: mark_draft_sent!
UPDATE email_drafts
SET status = 'sent', sent_at = now(), updated_at = now()
WHERE id = :id;

-- name: mark_draft_discarded!
UPDATE email_drafts
SET status = 'discarded', updated_at = now()
WHERE id = :id;
