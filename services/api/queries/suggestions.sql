-- ============================================================
-- Agent Suggestions
-- ============================================================

-- name: create_suggestion^
INSERT INTO agent_suggestions (
    id, coordinator_email, gmail_message_id, gmail_thread_id,
    loop_id, stage_id, classification, action, auto_advance,
    confidence, summary, target_state, extracted_entities,
    questions, action_data, reasoning, status
)
VALUES (
    :id, :coordinator_email, :gmail_message_id, :gmail_thread_id,
    :loop_id, :stage_id, :classification, :action, :auto_advance,
    :confidence, :summary, :target_state, :extracted_entities,
    :questions, :action_data, :reasoning, :status
)
RETURNING id, coordinator_email, gmail_message_id, gmail_thread_id,
          loop_id, stage_id, classification, action, auto_advance,
          confidence, summary, target_state, extracted_entities,
          questions, action_data, reasoning, status, resolved_at,
          resolved_by, created_at;

-- name: get_suggestion^
SELECT id, coordinator_email, gmail_message_id, gmail_thread_id,
       loop_id, stage_id, classification, action, auto_advance,
       confidence, summary, target_state, extracted_entities,
       questions, action_data, reasoning, status, resolved_at,
       resolved_by, created_at
FROM agent_suggestions
WHERE id = :id;

-- name: get_suggestions_for_thread
SELECT id, coordinator_email, gmail_message_id, gmail_thread_id,
       loop_id, stage_id, classification, action, auto_advance,
       confidence, summary, target_state, extracted_entities,
       questions, action_data, reasoning, status, resolved_at,
       resolved_by, created_at
FROM agent_suggestions
WHERE gmail_thread_id = :gmail_thread_id
ORDER BY created_at DESC;

-- name: get_pending_suggestions_for_coordinator
SELECT id, coordinator_email, gmail_message_id, gmail_thread_id,
       loop_id, stage_id, classification, action, auto_advance,
       confidence, summary, target_state, extracted_entities,
       questions, action_data, reasoning, status, resolved_at,
       resolved_by, created_at
FROM agent_suggestions
WHERE coordinator_email = :coordinator_email AND status = 'pending'
ORDER BY created_at DESC;

-- name: get_pending_suggestions_for_loop
SELECT id, coordinator_email, gmail_message_id, gmail_thread_id,
       loop_id, stage_id, classification, action, auto_advance,
       confidence, summary, target_state, extracted_entities,
       questions, action_data, reasoning, status, resolved_at,
       resolved_by, created_at
FROM agent_suggestions
WHERE loop_id = :loop_id AND status = 'pending'
ORDER BY created_at DESC;

-- name: resolve_suggestion!
UPDATE agent_suggestions
SET status = :status, resolved_at = now(), resolved_by = :resolved_by
WHERE id = :id AND status = 'pending';

-- name: supersede_pending_suggestions_for_loop!
-- Mark all pending suggestions for a loop as superseded (outgoing email invalidated them).
UPDATE agent_suggestions
SET status = 'superseded', resolved_at = now(), resolved_by = :resolved_by
WHERE loop_id = :loop_id AND status = 'pending';

-- name: expire_old_suggestions!
-- Expire pending suggestions older than the given cutoff.
UPDATE agent_suggestions
SET status = 'expired', resolved_at = now()
WHERE status = 'pending' AND created_at < :cutoff;
