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
INSERT INTO contacts (id, name, email, role, company, photo_url)
VALUES (:id, :name, :email, :role, :company, :photo_url)
RETURNING id, name, email, role, company, photo_url, created_at;

-- name: get_contact^
SELECT id, name, email, role, company, photo_url, created_at
FROM contacts WHERE id = :id;

-- name: get_contact_by_email_and_role^
-- Used to dedupe on loop creation: reuse an existing contact if the
-- (email, role) pair already exists instead of inserting a duplicate.
SELECT id, name, email, role, company, photo_url, created_at
FROM contacts
WHERE email = :email AND role = :role
LIMIT 1;

-- name: search_contacts_by_prefix
-- Autocomplete: search contacts by name prefix, optionally filtered by role.
SELECT id, name, email, role, company, photo_url, created_at
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

-- name: get_client_contact_by_email^
-- Used to dedupe on loop creation: reuse an existing client contact if
-- the email already exists instead of inserting a duplicate.
SELECT id, name, email, company, created_at
FROM client_contacts
WHERE email = :email
LIMIT 1;

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
INSERT INTO loops (id, coordinator_id, client_contact_id, recruiter_id, client_manager_id, candidate_id, title, state, notes)
VALUES (:id, :coordinator_id, :client_contact_id, :recruiter_id, :client_manager_id, :candidate_id, :title, :state, :notes)
RETURNING id, coordinator_id, client_contact_id, recruiter_id, client_manager_id, candidate_id, title, notes, state, created_at, updated_at;

-- name: get_loop^
SELECT id, coordinator_id, client_contact_id, recruiter_id, client_manager_id, candidate_id, title, notes, state, created_at, updated_at
FROM loops WHERE id = :id;

-- name: get_loop_full^
-- Fetch a loop with all actor details in a single query.
-- Coordinator and candidate are INNER JOIN — both FKs are NOT NULL on loops
-- (see migration 0002), so a missing actor row would mean DB corruption.
-- Client contact, recruiter, and client manager are LEFT JOIN — those FKs
-- became nullable in migration 0009 (loops can be auto-created with
-- incomplete contact info; missing pieces are collected JIT at send time).
SELECT
    l.id                AS loop_id,
    l.coordinator_id    AS loop_coordinator_id,
    l.client_contact_id AS loop_client_contact_id,
    l.recruiter_id      AS loop_recruiter_id,
    l.client_manager_id AS loop_client_manager_id,
    l.candidate_id      AS loop_candidate_id,
    l.title             AS loop_title,
    l.notes             AS loop_notes,
    l.state             AS loop_state,
    l.created_at        AS loop_created_at,
    l.updated_at        AS loop_updated_at,
    co.name             AS coord_name,
    co.email            AS coord_email,
    co.created_at       AS coord_created_at,
    cc.name             AS cc_name,
    cc.email            AS cc_email,
    cc.company          AS cc_company,
    cc.created_at       AS cc_created_at,
    rec.name            AS rec_name,
    rec.email           AS rec_email,
    rec.role            AS rec_role,
    rec.company         AS rec_company,
    rec.photo_url       AS rec_photo_url,
    rec.created_at      AS rec_created_at,
    cm.name             AS cm_name,
    cm.email            AS cm_email,
    cm.role             AS cm_role,
    cm.company          AS cm_company,
    cm.photo_url        AS cm_photo_url,
    cm.created_at       AS cm_created_at,
    cand.name           AS cand_name,
    cand.notes          AS cand_notes,
    cand.created_at     AS cand_created_at
FROM loops l
JOIN coordinators co ON co.id = l.coordinator_id
LEFT JOIN client_contacts cc ON cc.id = l.client_contact_id
LEFT JOIN contacts rec ON rec.id = l.recruiter_id
LEFT JOIN contacts cm ON cm.id = l.client_manager_id
JOIN candidates cand ON cand.id = l.candidate_id
WHERE l.id = :id;

-- name: get_loops_full_for_coordinator
-- All loops for a coordinator with actor details populated (for status board).
SELECT
    l.id                AS loop_id,
    l.coordinator_id    AS loop_coordinator_id,
    l.client_contact_id AS loop_client_contact_id,
    l.recruiter_id      AS loop_recruiter_id,
    l.client_manager_id AS loop_client_manager_id,
    l.candidate_id      AS loop_candidate_id,
    l.title             AS loop_title,
    l.notes             AS loop_notes,
    l.state             AS loop_state,
    l.created_at        AS loop_created_at,
    l.updated_at        AS loop_updated_at,
    co.name             AS coord_name,
    co.email            AS coord_email,
    co.created_at       AS coord_created_at,
    cc.name             AS cc_name,
    cc.email            AS cc_email,
    cc.company          AS cc_company,
    cc.created_at       AS cc_created_at,
    rec.name            AS rec_name,
    rec.email           AS rec_email,
    rec.role            AS rec_role,
    rec.company         AS rec_company,
    rec.photo_url       AS rec_photo_url,
    rec.created_at      AS rec_created_at,
    cm.name             AS cm_name,
    cm.email            AS cm_email,
    cm.role             AS cm_role,
    cm.company          AS cm_company,
    cm.photo_url        AS cm_photo_url,
    cm.created_at       AS cm_created_at,
    cand.name           AS cand_name,
    cand.notes          AS cand_notes,
    cand.created_at     AS cand_created_at
FROM loops l
JOIN coordinators co ON co.id = l.coordinator_id
LEFT JOIN client_contacts cc ON cc.id = l.client_contact_id
LEFT JOIN contacts rec ON rec.id = l.recruiter_id
LEFT JOIN contacts cm ON cm.id = l.client_manager_id
JOIN candidates cand ON cand.id = l.candidate_id
WHERE l.coordinator_id = :coordinator_id
ORDER BY l.updated_at DESC;

-- name: get_active_loops_full_for_coordinator
-- Active loops (not complete or cold) for a coordinator, with actors.
SELECT
    l.id                AS loop_id,
    l.coordinator_id    AS loop_coordinator_id,
    l.client_contact_id AS loop_client_contact_id,
    l.recruiter_id      AS loop_recruiter_id,
    l.client_manager_id AS loop_client_manager_id,
    l.candidate_id      AS loop_candidate_id,
    l.title             AS loop_title,
    l.notes             AS loop_notes,
    l.state             AS loop_state,
    l.created_at        AS loop_created_at,
    l.updated_at        AS loop_updated_at,
    co.name             AS coord_name,
    co.email            AS coord_email,
    co.created_at       AS coord_created_at,
    cc.name             AS cc_name,
    cc.email            AS cc_email,
    cc.company          AS cc_company,
    cc.created_at       AS cc_created_at,
    rec.name            AS rec_name,
    rec.email           AS rec_email,
    rec.role            AS rec_role,
    rec.company         AS rec_company,
    rec.photo_url       AS rec_photo_url,
    rec.created_at      AS rec_created_at,
    cm.name             AS cm_name,
    cm.email            AS cm_email,
    cm.role             AS cm_role,
    cm.company          AS cm_company,
    cm.photo_url        AS cm_photo_url,
    cm.created_at       AS cm_created_at,
    cand.name           AS cand_name,
    cand.notes          AS cand_notes,
    cand.created_at     AS cand_created_at
FROM loops l
JOIN coordinators co ON co.id = l.coordinator_id
LEFT JOIN client_contacts cc ON cc.id = l.client_contact_id
LEFT JOIN contacts rec ON rec.id = l.recruiter_id
LEFT JOIN contacts cm ON cm.id = l.client_manager_id
JOIN candidates cand ON cand.id = l.candidate_id
WHERE l.coordinator_id = :coordinator_id
  AND l.state NOT IN ('complete', 'cold')
ORDER BY l.updated_at DESC;

-- name: get_loops_for_coordinator
-- All active loops for a coordinator (not complete/cold).
SELECT l.id, l.coordinator_id, l.client_contact_id, l.recruiter_id,
       l.client_manager_id, l.candidate_id, l.title, l.notes, l.state, l.created_at, l.updated_at
FROM loops l
WHERE l.coordinator_id = :coordinator_id
  AND l.state NOT IN ('complete', 'cold')
ORDER BY l.updated_at DESC;

-- name: get_all_loops_for_coordinator
-- All loops for a coordinator (including complete/cold), for the full board.
SELECT l.id, l.coordinator_id, l.client_contact_id, l.recruiter_id,
       l.client_manager_id, l.candidate_id, l.title, l.notes, l.state, l.created_at, l.updated_at
FROM loops l
WHERE l.coordinator_id = :coordinator_id
ORDER BY l.updated_at DESC;

-- name: update_loop_state!
UPDATE loops SET state = :state, updated_at = now() WHERE id = :id;

-- name: update_loop_timestamp!
UPDATE loops SET updated_at = now() WHERE id = :id;

-- name: set_loop_recruiter!
-- Patch a loop's recruiter_id. Used when JIT contact info is supplied at
-- send time on a loop that was created without a recruiter.
UPDATE loops SET recruiter_id = :recruiter_id, updated_at = now() WHERE id = :id;

-- name: set_loop_client_contact!
-- Patch a loop's client_contact_id. Same JIT pattern as set_loop_recruiter.
UPDATE loops SET client_contact_id = :client_contact_id, updated_at = now() WHERE id = :id;

-- name: set_loop_client_manager!
-- Patch a loop's client_manager_id (already nullable since 0002).
UPDATE loops SET client_manager_id = :client_manager_id, updated_at = now() WHERE id = :id;

-- name: update_candidate_name!
-- Rename a candidate. Used by the inline rename affordance when the
-- classifier auto-resolved CREATE_LOOP with the placeholder
-- "Unknown Candidate".
UPDATE candidates SET name = :name WHERE id = :id;

-- ============================================================
-- Loop events
-- ============================================================

-- name: insert_event^
INSERT INTO loop_events (id, loop_id, event_type, data, actor_email)
VALUES (:id, :loop_id, :event_type, :data, :actor_email)
RETURNING id, loop_id, event_type, data, actor_email, occurred_at;

-- name: get_events_for_loop
SELECT id, loop_id, event_type, data, actor_email, occurred_at
FROM loop_events
WHERE loop_id = :loop_id
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
-- Multi-loop threads are possible — this returns the first one (legacy
-- single-loop callers). New code should prefer find_loops_by_gmail_thread_id.
SELECT l.id, l.coordinator_id, l.client_contact_id, l.recruiter_id,
       l.client_manager_id, l.candidate_id, l.title, l.notes, l.state, l.created_at, l.updated_at
FROM loops l
JOIN loop_email_threads let ON let.loop_id = l.id
WHERE let.gmail_thread_id = :gmail_thread_id
LIMIT 1;

-- name: find_loops_by_gmail_thread_id
-- All loops linked to a Gmail thread. Returns ALL loops regardless of state —
-- callers (the next-action agent) need to see cold/complete loops too in case
-- they need to reopen one.
SELECT l.id, l.coordinator_id, l.client_contact_id, l.recruiter_id,
       l.client_manager_id, l.candidate_id, l.title, l.notes, l.state, l.created_at, l.updated_at
FROM loops l
JOIN loop_email_threads let ON let.loop_id = l.id
WHERE let.gmail_thread_id = :gmail_thread_id
ORDER BY l.created_at;

-- ============================================================
-- Batch queries (for multi-loop operations)
-- ============================================================

-- name: get_threads_for_loops
-- All email threads for a set of loop IDs.
SELECT id, loop_id, gmail_thread_id, subject, linked_at
FROM loop_email_threads
WHERE loop_id = ANY(:loop_ids)
ORDER BY loop_id, linked_at;
