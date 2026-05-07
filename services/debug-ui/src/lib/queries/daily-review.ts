import { query } from "@/lib/db";
import type {
  DraftStatus,
  EmailClassification,
  StageState,
  SuggestedAction,
  SuggestionStatus,
} from "@/lib/types";
import { etDayToUtcRange } from "@/lib/reviews/time";

/**
 * Daily-review row: one Suggestion + the resolved loop's actor context
 * (useful for create_loop "did the extractor get it right?") + the linked
 * draft (only for draft_email).
 *
 * Returned for ALL three relevant actions in chronological order. The UI
 * splits them into Part 1 (create_loop) and Part 2 (draft_email +
 * ask_coordinator).
 */
export interface DailyReviewItem {
  // suggestion
  id: string;
  coordinator_email: string;
  gmail_message_id: string;
  gmail_thread_id: string;
  loop_id: string | null;
  classification: EmailClassification;
  action: SuggestedAction;
  confidence: number;
  summary: string;
  action_data: Record<string, unknown> | null;
  reasoning: string | null;
  status: SuggestionStatus;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
  // resolved loop context (populated when loop_id resolves)
  loop_title: string | null;
  loop_state: StageState | null;
  candidate_name: string | null;
  client_contact_name: string | null;
  client_contact_email: string | null;
  client_company: string | null;
  recruiter_name: string | null;
  recruiter_email: string | null;
  client_manager_name: string | null;
  client_manager_email: string | null;
  // draft (only for draft_email)
  draft_id: string | null;
  draft_to_emails: string[] | null;
  draft_cc_emails: string[] | null;
  draft_subject: string | null;
  draft_body: string | null;
  draft_status: DraftStatus | null;
  draft_is_forward: boolean | null;
}

/**
 * Fetch all reviewable suggestions for a coordinator on a given ET day.
 *
 * "Reviewable" = action in (create_loop, draft_email, ask_coordinator).
 * We include all statuses; the reviewer is auditing the agent's output,
 * not just what the coordinator approved.
 */
export async function getDailyReviewItems(
  coordinatorEmail: string,
  date: string
): Promise<DailyReviewItem[]> {
  const [startUtc, endUtc] = etDayToUtcRange(date);
  return query<DailyReviewItem>(
    `SELECT
      s.id, s.coordinator_email, s.gmail_message_id, s.gmail_thread_id,
      s.loop_id, s.classification, s.action,
      s.confidence, s.summary, s.action_data, s.reasoning, s.status,
      s.resolved_at, s.resolved_by, s.created_at,
      l.title  AS loop_title,
      l.state  AS loop_state,
      cand.name AS candidate_name,
      cc.name  AS client_contact_name,
      cc.email AS client_contact_email,
      cc.company AS client_company,
      rec.name AS recruiter_name,
      rec.email AS recruiter_email,
      cm.name  AS client_manager_name,
      cm.email AS client_manager_email,
      d.id     AS draft_id,
      d.to_emails AS draft_to_emails,
      d.cc_emails AS draft_cc_emails,
      d.subject AS draft_subject,
      d.body    AS draft_body,
      d.status  AS draft_status,
      d.is_forward AS draft_is_forward
    FROM agent_suggestions s
    LEFT JOIN loops l            ON s.loop_id = l.id
    LEFT JOIN candidates cand    ON l.candidate_id = cand.id
    LEFT JOIN client_contacts cc ON l.client_contact_id = cc.id
    LEFT JOIN contacts rec       ON l.recruiter_id = rec.id
    LEFT JOIN contacts cm        ON l.client_manager_id = cm.id
    LEFT JOIN email_drafts d     ON d.suggestion_id = s.id
    WHERE s.coordinator_email = $1
      AND s.created_at >= $2::timestamptz
      AND s.created_at <  $3::timestamptz
      AND s.action IN ('create_loop','draft_email','ask_coordinator')
    ORDER BY s.created_at ASC`,
    [coordinatorEmail, startUtc, endUtc]
  );
}

/**
 * Lighter variant for the landing page: counts per day for the last N
 * ET days, no row payload. One round trip via UNION ALL is plenty for 14
 * days × 3 actions.
 */
export async function getDailyReviewCounts(
  coordinatorEmail: string,
  dates: string[]
): Promise<Map<string, number>> {
  if (dates.length === 0) return new Map();

  // Build a single query: for each date, count matching suggestions in
  // its ET window. We do this client-side as N parallel queries because
  // composing N timezone-correct windows in pure SQL would require a
  // lot of CASE expressions and AT TIME ZONE math; N=14 is fine.
  const results = await Promise.all(
    dates.map(async (date) => {
      const [startUtc, endUtc] = etDayToUtcRange(date);
      const rows = await query<{ count: string }>(
        `SELECT COUNT(*)::text AS count
         FROM agent_suggestions
         WHERE coordinator_email = $1
           AND created_at >= $2::timestamptz
           AND created_at <  $3::timestamptz
           AND action IN ('create_loop','draft_email','ask_coordinator')`,
        [coordinatorEmail, startUtc, endUtc]
      );
      return [date, parseInt(rows[0]?.count ?? "0", 10)] as const;
    })
  );

  return new Map(results);
}
