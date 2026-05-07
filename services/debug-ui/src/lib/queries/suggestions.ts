import { query } from "@/lib/db";
import type {
  Suggestion,
  SuggestionWithContext,
  LoopSuggestionGroup,
  EmailDraft,
  SuggestedAction,
  DraftStatus,
  SuggestionForLoop,
  StageState,
} from "@/lib/types";

interface RawSuggestionRow {
  // suggestion columns
  id: string;
  coordinator_email: string;
  gmail_message_id: string;
  gmail_thread_id: string;
  loop_id: string | null;
  classification: string;
  action: string;
  confidence: number;
  summary: string;
  action_data: Record<string, unknown> | null;
  reasoning: string | null;
  status: string;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
  // loop context
  loop_title: string | null;
  loop_state: string | null;
  candidate_name: string | null;
  client_company: string | null;
  // draft context
  draft_id: string | null;
  draft_to_emails: string[] | null;
  draft_cc_emails: string[] | null;
  draft_subject: string | null;
  draft_body: string | null;
  draft_status: string | null;
  draft_gmail_thread_id: string | null;
  draft_is_forward: boolean | null;
  draft_pending_jit_data: Record<string, unknown> | null;
  // actor context
  client_contact_name: string | null;
  client_contact_email: string | null;
  recruiter_name: string | null;
  recruiter_email: string | null;
  client_manager_name: string | null;
  client_manager_email: string | null;
}

function rowToSuggestionWithContext(
  row: RawSuggestionRow
): SuggestionWithContext {
  const suggestion: Suggestion = {
    id: row.id,
    coordinator_email: row.coordinator_email,
    gmail_message_id: row.gmail_message_id,
    gmail_thread_id: row.gmail_thread_id,
    loop_id: row.loop_id,
    classification: row.classification as Suggestion["classification"],
    action: row.action as SuggestedAction,
    confidence: row.confidence,
    summary: row.summary,
    action_data: row.action_data,
    reasoning: row.reasoning,
    status: row.status as Suggestion["status"],
    resolved_at: row.resolved_at,
    resolved_by: row.resolved_by,
    created_at: row.created_at,
  };

  let draft: EmailDraft | null = null;
  if (row.draft_id) {
    draft = {
      id: row.draft_id,
      suggestion_id: suggestion.id,
      loop_id: suggestion.loop_id || "",
      coordinator_email: suggestion.coordinator_email,
      to_emails: row.draft_to_emails || [],
      cc_emails: row.draft_cc_emails || [],
      subject: row.draft_subject || "",
      body: row.draft_body || "",
      status: (row.draft_status as DraftStatus) || "generated",
      gmail_thread_id: row.draft_gmail_thread_id,
      is_forward: row.draft_is_forward ?? false,
      pending_jit_data: row.draft_pending_jit_data || {},
    };
  }

  return {
    suggestion,
    loop_title: row.loop_title,
    loop_state: row.loop_state as StageState | null,
    candidate_name: row.candidate_name,
    client_company: row.client_company,
    draft,
    client_contact_name: row.client_contact_name,
    client_contact_email: row.client_contact_email,
    recruiter_name: row.recruiter_name,
    recruiter_email: row.recruiter_email,
    client_manager_name: row.client_manager_name,
    client_manager_email: row.client_manager_email,
  };
}

export async function getPendingSuggestionsWithContext(
  coordinatorEmail: string
): Promise<SuggestionWithContext[]> {
  const rows = await query<RawSuggestionRow>(
    `SELECT
      s.id, s.coordinator_email, s.gmail_message_id, s.gmail_thread_id,
      s.loop_id, s.classification, s.action,
      s.confidence, s.summary, s.action_data, s.reasoning, s.status,
      s.resolved_at, s.resolved_by, s.created_at,
      l.title AS loop_title,
      l.state AS loop_state,
      cand.name AS candidate_name,
      cc.company AS client_company,
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
    LEFT JOIN email_drafts d ON d.suggestion_id = s.id
      AND d.status IN ('generated', 'edited')
    WHERE s.coordinator_email = $1
      AND s.status = 'pending'
      AND s.action != 'no_action'
    ORDER BY s.created_at ASC`,
    [coordinatorEmail]
  );
  return rows.map(rowToSuggestionWithContext);
}

export function groupByLoop(
  views: SuggestionWithContext[]
): LoopSuggestionGroup[] {
  const groups = new Map<string | null, LoopSuggestionGroup>();

  for (const v of views) {
    const key = v.suggestion.loop_id;
    if (!groups.has(key)) {
      groups.set(key, {
        loop_id: key,
        loop_title: v.loop_title,
        candidate_name: v.candidate_name,
        client_company: v.client_company,
        suggestions: [],
        oldest_created_at: v.suggestion.created_at,
      });
    }
    groups.get(key)!.suggestions.push(v);
  }

  for (const group of groups.values()) {
    group.suggestions.sort((a, b) => {
      const aIsAdvance = a.suggestion.action === "advance_stage" ? 1 : 0;
      const bIsAdvance = b.suggestion.action === "advance_stage" ? 1 : 0;
      if (aIsAdvance !== bIsAdvance) return aIsAdvance - bIsAdvance;
      return (
        new Date(a.suggestion.created_at).getTime() -
        new Date(b.suggestion.created_at).getTime()
      );
    });
  }

  return [...groups.values()].sort(
    (a, b) =>
      new Date(a.oldest_created_at).getTime() -
      new Date(b.oldest_created_at).getTime()
  );
}

export async function getSuggestionsForLoop(
  loopId: string
): Promise<SuggestionForLoop[]> {
  return query<SuggestionForLoop>(
    `SELECT
      s.id, s.coordinator_email, s.gmail_message_id, s.gmail_thread_id,
      s.loop_id, s.classification, s.action,
      s.confidence, s.summary, s.action_data, s.reasoning, s.status,
      s.resolved_at, s.resolved_by, s.created_at,
      d.id AS draft_id, d.subject AS draft_subject, d.body AS draft_body,
      d.status AS draft_status, d.to_emails AS draft_to_emails,
      d.cc_emails AS draft_cc_emails, d.is_forward AS draft_is_forward
    FROM agent_suggestions s
    LEFT JOIN email_drafts d ON d.suggestion_id = s.id
    WHERE s.loop_id = $1
    ORDER BY s.created_at DESC`,
    [loopId]
  );
}
