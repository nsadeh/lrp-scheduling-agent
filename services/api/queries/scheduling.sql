-- ============================================================
-- Coordinators
-- ============================================================

-- name: get_or_create_coordinator^
-- Upsert coordinator by email. Returns the coordinator row.
INSERT INTO coordinators (id, name, email)
VALUES (:id, :name, :email)
ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
RETURNING id, name, email, created_at;

-- name: get_coordinator_by_email^
SELECT id, name, email, created_at
FROM coordinators
WHERE email = :email;

-- name: get_coordinator^
SELECT id, name, email, created_at
FROM coordinators
WHERE id = :id;

-- ============================================================
-- Contacts (recruiters, client managers)
-- ============================================================

-- name: create_contact^
INSERT INTO contacts (id, name, email, role, company)
VALUES (:id, :name, :email, :role, :company)
RETURNING id, name, email, role, company, created_at;

-- name: get_contact^
SELECT id, name, email, role, company, created_at
FROM contacts WHERE id = :id;

-- name: search_contacts_by_prefix
-- Autocomplete: search contacts by name prefix, optionally filtered by role.
SELECT id, name, email, role, company, created_at
FROM contacts
WHERE name ILIKE :pattern
  AND (:role::TEXT IS NULL OR role = :role)
ORDER BY name
LIMIT 10;

-- ============================================================
-- Client contacts
-- ============================================================

-- name: create_client_contact^
INSERT INTO client_contacts (id, name, email, company)
VALUES (:id, :name, :email, :company)
RETURNING id, name, email, company, created_at;

-- name: get_client_contact^
SELECT id, name, email, company, created_at
FROM client_contacts WHERE id = :id;

-- name: search_client_contacts_by_prefix
SELECT id, name, email, company, created_at
FROM client_contacts
WHERE name ILIKE :pattern
ORDER BY name
LIMIT 10;

-- ============================================================
-- Candidates
-- ============================================================

-- name: create_candidate^
INSERT INTO candidates (id, name, notes)
VALUES (:id, :name, :notes)
RETURNING id, name, notes, created_at;

-- name: get_candidate^
SELECT id, name, notes, created_at
FROM candidates WHERE id = :id;

-- name: search_candidates_by_prefix
SELECT id, name, notes, created_at
FROM candidates
WHERE name ILIKE :pattern
ORDER BY name
LIMIT 10;

-- ============================================================
-- Loops
-- ============================================================

-- name: create_loop^
INSERT INTO loops (id, coordinator_id, client_contact_id, recruiter_id, client_manager_id, candidate_id, title, notes)
VALUES (:id, :coordinator_id, :client_contact_id, :recruiter_id, :client_manager_id, :candidate_id, :title, :notes)
RETURNING id, coordinator_id, client_contact_id, recruiter_id, client_manager_id, candidate_id, title, notes, created_at, updated_at;

-- name: get_loop^
SELECT id, coordinator_id, client_contact_id, recruiter_id, client_manager_id, candidate_id, title, notes, created_at, updated_at
FROM loops WHERE id = :id;

-- name: get_loops_for_coordinator
-- All loops for a coordinator that have at least one active stage.
SELECT DISTINCT l.id, l.coordinator_id, l.client_contact_id, l.recruiter_id,
       l.client_manager_id, l.candidate_id, l.title, l.notes, l.created_at, l.updated_at
FROM loops l
JOIN stages s ON s.loop_id = l.id
WHERE l.coordinator_id = :coordinator_id
  AND s.state NOT IN ('complete', 'cold')
ORDER BY l.updated_at DESC;

-- name: get_all_loops_for_coordinator
-- All loops for a coordinator (including complete/cold), for the full board.
SELECT l.id, l.coordinator_id, l.client_contact_id, l.recruiter_id,
       l.client_manager_id, l.candidate_id, l.title, l.notes, l.created_at, l.updated_at
FROM loops l
WHERE l.coordinator_id = :coordinator_id
ORDER BY l.updated_at DESC;

-- name: update_loop_timestamp!
UPDATE loops SET updated_at = now() WHERE id = :id;

-- ============================================================
-- Stages
-- ============================================================

-- name: create_stage^
INSERT INTO stages (id, loop_id, name, state, ordinal)
VALUES (:id, :loop_id, :name, :state, :ordinal)
RETURNING id, loop_id, name, state, ordinal, created_at, updated_at;

-- name: get_stage^
SELECT id, loop_id, name, state, ordinal, created_at, updated_at
FROM stages WHERE id = :id;

-- name: get_stages_for_loop
SELECT id, loop_id, name, state, ordinal, created_at, updated_at
FROM stages
WHERE loop_id = :loop_id
ORDER BY ordinal, created_at;

-- name: update_stage_state!
UPDATE stages SET state = :state, updated_at = now()
WHERE id = :id;

-- name: get_max_ordinal_for_loop$
SELECT COALESCE(MAX(ordinal), -1) AS max_ordinal
FROM stages WHERE loop_id = :loop_id;

-- ============================================================
-- Loop events
-- ============================================================

-- name: insert_event^
INSERT INTO loop_events (id, loop_id, stage_id, event_type, data, actor_email)
VALUES (:id, :loop_id, :stage_id, :event_type, :data, :actor_email)
RETURNING id, loop_id, stage_id, event_type, data, actor_email, occurred_at;

-- name: get_events_for_loop
SELECT id, loop_id, stage_id, event_type, data, actor_email, occurred_at
FROM loop_events
WHERE loop_id = :loop_id
ORDER BY occurred_at;

-- name: get_events_for_stage
SELECT id, loop_id, stage_id, event_type, data, actor_email, occurred_at
FROM loop_events
WHERE stage_id = :stage_id
ORDER BY occurred_at;

-- ============================================================
-- Email threads
-- ============================================================

-- name: link_thread^
INSERT INTO loop_email_threads (id, loop_id, gmail_thread_id, subject)
VALUES (:id, :loop_id, :gmail_thread_id, :subject)
ON CONFLICT (loop_id, gmail_thread_id) DO NOTHING
RETURNING id, loop_id, gmail_thread_id, subject, linked_at;

-- name: get_threads_for_loop
SELECT id, loop_id, gmail_thread_id, subject, linked_at
FROM loop_email_threads
WHERE loop_id = :loop_id
ORDER BY linked_at;

-- name: find_loop_by_gmail_thread_id^
-- Find the loop linked to a Gmail thread. Returns NULL if not linked.
SELECT l.id, l.coordinator_id, l.client_contact_id, l.recruiter_id,
       l.client_manager_id, l.candidate_id, l.title, l.notes, l.created_at, l.updated_at
FROM loops l
JOIN loop_email_threads let ON let.loop_id = l.id
WHERE let.gmail_thread_id = :gmail_thread_id
LIMIT 1;

-- ============================================================
-- Time slots
-- ============================================================

-- name: create_time_slot^
INSERT INTO time_slots (id, stage_id, start_time, duration_minutes, timezone, zoom_link, notes)
VALUES (:id, :stage_id, :start_time, :duration_minutes, :timezone, :zoom_link, :notes)
RETURNING id, stage_id, start_time, duration_minutes, timezone, zoom_link, notes, created_at;

-- name: get_time_slots_for_stage
SELECT id, stage_id, start_time, duration_minutes, timezone, zoom_link, notes, created_at
FROM time_slots
WHERE stage_id = :stage_id
ORDER BY start_time;

-- name: get_time_slots_for_loop
SELECT ts.id, ts.stage_id, ts.start_time, ts.duration_minutes, ts.timezone,
       ts.zoom_link, ts.notes, ts.created_at
FROM time_slots ts
JOIN stages s ON s.id = ts.stage_id
WHERE s.loop_id = :loop_id
ORDER BY ts.start_time;
