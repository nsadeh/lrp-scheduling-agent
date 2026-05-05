-- ============================================================
-- Email Drafts
-- ============================================================

-- name: create_draft^
INSERT INTO email_drafts (
    id, suggestion_id, loop_id, coordinator_email,
    to_emails, cc_emails, subject, body, gmail_thread_id, is_forward, status
)
VALUES (
    :id, :suggestion_id, :loop_id, :coordinator_email,
    :to_emails, :cc_emails, :subject, :body, :gmail_thread_id, :is_forward, :status
)
RETURNING id, suggestion_id, loop_id, coordinator_email,
          to_emails, cc_emails, subject, body, gmail_thread_id,
          is_forward, status, pending_jit_data, sent_at, created_at, updated_at;

-- name: get_draft^
SELECT id, suggestion_id, loop_id, coordinator_email,
       to_emails, cc_emails, subject, body, gmail_thread_id,
       is_forward, status, pending_jit_data, sent_at, created_at, updated_at
FROM email_drafts
WHERE id = :id;

-- name: get_draft_for_suggestion^
SELECT id, suggestion_id, loop_id, coordinator_email,
       to_emails, cc_emails, subject, body, gmail_thread_id,
       is_forward, status, pending_jit_data, sent_at, created_at, updated_at
FROM email_drafts
WHERE suggestion_id = :suggestion_id;

-- name: get_pending_drafts_for_coordinator
SELECT id, suggestion_id, loop_id, coordinator_email,
       to_emails, cc_emails, subject, body, gmail_thread_id,
       is_forward, status, pending_jit_data, sent_at, created_at, updated_at
FROM email_drafts
WHERE coordinator_email = :coordinator_email
  AND status IN ('generated', 'edited')
ORDER BY created_at DESC;

-- name: update_draft_body!
UPDATE email_drafts
SET body = :body, status = 'edited', updated_at = now()
WHERE id = :id;

-- name: update_draft_recipients!
-- Patch to_emails / cc_emails after JIT contact info is supplied at send time.
UPDATE email_drafts
SET to_emails = :to_emails, cc_emails = :cc_emails, updated_at = now()
WHERE id = :id;

-- name: update_pending_jit_data!
-- Replace the draft's pending_jit_data wholesale. Used when the coordinator
-- picks a JIT contact (recruiter / client / CM) — we stash the pick here
-- instead of committing to the loop, so misclicks can be undone with the
-- "x" clear button before send.
UPDATE email_drafts
SET pending_jit_data = :pending_jit_data, updated_at = now()
WHERE id = :id;

-- name: mark_draft_sent!
UPDATE email_drafts
SET status = 'sent', sent_at = now(), updated_at = now()
WHERE id = :id;

-- name: mark_draft_discarded!
UPDATE email_drafts
SET status = 'discarded', updated_at = now()
WHERE id = :id;
