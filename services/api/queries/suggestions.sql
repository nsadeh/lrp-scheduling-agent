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

-- name: get_pending_suggestions_with_context
-- Denormalized query for the overview card: suggestions + loop context + draft context.
-- Returns everything the UI needs in a single round-trip (no N+1).
SELECT
    s.id, s.coordinator_email, s.gmail_message_id, s.gmail_thread_id,
    s.loop_id, s.stage_id, s.classification, s.action, s.auto_advance,
    s.confidence, s.summary, s.target_state, s.extracted_entities,
    s.questions, s.action_data, s.reasoning, s.status, s.resolved_at,
    s.resolved_by, s.created_at,
    -- Loop context (nullable for CREATE_LOOP)
    l.title AS loop_title,
    cand.name AS candidate_name,
    cc.company AS client_company,
    -- Stage context (nullable — only present when suggestion has a stage_id)
    stg.name AS stage_name,
    stg.state AS stage_state,
    -- Draft context (nullable for non-DRAFT_EMAIL)
    d.id AS draft_id, d.to_emails AS draft_to_emails,
    d.cc_emails AS draft_cc_emails, d.subject AS draft_subject,
    d.body AS draft_body, d.status AS draft_status,
    d.gmail_thread_id AS draft_gmail_thread_id,
    d.is_forward AS draft_is_forward,
    d.pending_jit_data AS draft_pending_jit_data,
    -- Known actor emails — used as small-print hints under JIT inputs so
    -- coordinators can see what we already have when we ask for the missing one.
    cc.name AS client_contact_name,
    cc.email AS client_contact_email,
    rec.name AS recruiter_name,
    rec.email AS recruiter_email,
    cm.name AS client_manager_name,
    cm.email AS client_manager_email
FROM agent_suggestions s
LEFT JOIN loops l ON s.loop_id = l.id
LEFT JOIN candidates cand ON l.candidate_id = cand.id
LEFT JOIN client_contacts cc ON l.client_contact_id = cc.id
LEFT JOIN contacts rec ON l.recruiter_id = rec.id
LEFT JOIN contacts cm ON l.client_manager_id = cm.id
LEFT JOIN stages stg ON s.stage_id = stg.id
LEFT JOIN email_drafts d ON d.suggestion_id = s.id
    AND d.status IN ('generated', 'edited')
WHERE s.coordinator_email = :coordinator_email
    AND s.status = 'pending'
    AND s.action != 'no_action'
ORDER BY s.created_at ASC;

-- name: get_pending_suggestions_for_thread_with_context
-- Same as above, but filtered to a specific Gmail thread.
SELECT
    s.id, s.coordinator_email, s.gmail_message_id, s.gmail_thread_id,
    s.loop_id, s.stage_id, s.classification, s.action, s.auto_advance,
    s.confidence, s.summary, s.target_state, s.extracted_entities,
    s.questions, s.action_data, s.reasoning, s.status, s.resolved_at,
    s.resolved_by, s.created_at,
    l.title AS loop_title,
    cand.name AS candidate_name,
    cc.company AS client_company,
    stg.name AS stage_name,
    stg.state AS stage_state,
    d.id AS draft_id, d.to_emails AS draft_to_emails,
    d.cc_emails AS draft_cc_emails, d.subject AS draft_subject,
    d.body AS draft_body, d.status AS draft_status,
    d.gmail_thread_id AS draft_gmail_thread_id,
    d.is_forward AS draft_is_forward,
    d.pending_jit_data AS draft_pending_jit_data,
    cc.name AS client_contact_name,
    cc.email AS client_contact_email,
    rec.name AS recruiter_name,
    rec.email AS recruiter_email,
    cm.name AS client_manager_name,
    cm.email AS client_manager_email
FROM agent_suggestions s
LEFT JOIN loops l ON s.loop_id = l.id
LEFT JOIN candidates cand ON l.candidate_id = cand.id
LEFT JOIN client_contacts cc ON l.client_contact_id = cc.id
LEFT JOIN contacts rec ON l.recruiter_id = rec.id
LEFT JOIN contacts cm ON l.client_manager_id = cm.id
LEFT JOIN stages stg ON s.stage_id = stg.id
LEFT JOIN email_drafts d ON d.suggestion_id = s.id
    AND d.status IN ('generated', 'edited')
WHERE s.gmail_thread_id = :gmail_thread_id
    AND s.coordinator_email = :coordinator_email
    AND s.status = 'pending'
    AND s.action != 'no_action'
ORDER BY s.created_at ASC;
