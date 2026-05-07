import { query, queryOne } from "@/lib/db";
import type { LoopFull, StageState } from "@/lib/types";

interface RawLoopRow {
  loop_id: string;
  loop_coordinator_id: string;
  loop_title: string;
  loop_notes: string | null;
  loop_state: StageState;
  loop_created_at: string;
  loop_updated_at: string;
  coord_name: string;
  coord_email: string;
  cand_name: string;
  cand_notes: string | null;
  cc_name: string | null;
  cc_email: string | null;
  cc_company: string | null;
  rec_name: string | null;
  rec_email: string | null;
  cm_name: string | null;
  cm_email: string | null;
}

function rowToLoopFull(row: RawLoopRow): LoopFull {
  return {
    id: row.loop_id,
    coordinator_id: row.loop_coordinator_id,
    title: row.loop_title,
    notes: row.loop_notes,
    state: row.loop_state,
    created_at: row.loop_created_at,
    updated_at: row.loop_updated_at,
    coord_name: row.coord_name,
    coord_email: row.coord_email,
    candidate_name: row.cand_name,
    candidate_notes: row.cand_notes,
    client_contact_name: row.cc_name,
    client_contact_email: row.cc_email,
    client_company: row.cc_company,
    recruiter_name: row.rec_name,
    recruiter_email: row.rec_email,
    client_manager_name: row.cm_name,
    client_manager_email: row.cm_email,
  };
}

const LOOPS_FULL_SELECT = `
  SELECT
    l.id                AS loop_id,
    l.coordinator_id    AS loop_coordinator_id,
    l.title             AS loop_title,
    l.notes             AS loop_notes,
    l.state             AS loop_state,
    l.created_at        AS loop_created_at,
    l.updated_at        AS loop_updated_at,
    co.name             AS coord_name,
    co.email            AS coord_email,
    cand.name           AS cand_name,
    cand.notes          AS cand_notes,
    cc.name             AS cc_name,
    cc.email            AS cc_email,
    cc.company          AS cc_company,
    rec.name            AS rec_name,
    rec.email           AS rec_email,
    cm.name             AS cm_name,
    cm.email            AS cm_email
  FROM loops l
  JOIN coordinators co ON co.id = l.coordinator_id
  LEFT JOIN client_contacts cc ON cc.id = l.client_contact_id
  LEFT JOIN contacts rec ON rec.id = l.recruiter_id
  LEFT JOIN contacts cm ON cm.id = l.client_manager_id
  JOIN candidates cand ON cand.id = l.candidate_id`;

export async function getAllLoopsForCoordinator(
  coordinatorId: string
): Promise<LoopFull[]> {
  const rows = await query<RawLoopRow>(
    `${LOOPS_FULL_SELECT}
     WHERE l.coordinator_id = $1
     ORDER BY l.updated_at DESC`,
    [coordinatorId]
  );
  return rows.map(rowToLoopFull);
}

export async function getActiveLoopsForCoordinator(
  coordinatorId: string
): Promise<LoopFull[]> {
  const rows = await query<RawLoopRow>(
    `${LOOPS_FULL_SELECT}
     WHERE l.coordinator_id = $1
       AND l.state NOT IN ('complete', 'cold')
     ORDER BY l.updated_at DESC`,
    [coordinatorId]
  );
  return rows.map(rowToLoopFull);
}

export async function getLoopFull(loopId: string): Promise<LoopFull | null> {
  const row = await queryOne<RawLoopRow>(
    `${LOOPS_FULL_SELECT} WHERE l.id = $1`,
    [loopId]
  );
  return row ? rowToLoopFull(row) : null;
}
