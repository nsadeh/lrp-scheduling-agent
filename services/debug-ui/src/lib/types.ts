export type StageState =
  | "new"
  | "awaiting_candidate"
  | "awaiting_client"
  | "scheduled"
  | "complete"
  | "cold";

export type SuggestionStatus =
  | "pending"
  | "accepted"
  | "rejected"
  | "expired"
  | "auto_applied"
  | "superseded";

export type SuggestedAction =
  | "advance_stage"
  | "create_loop"
  | "link_thread"
  | "draft_email"
  | "ask_coordinator"
  | "no_action";

export type EmailClassification =
  | "new_interview_request"
  | "availability_response"
  | "time_confirmation"
  | "reschedule_request"
  | "cancellation"
  | "follow_up_needed"
  | "informational"
  | "not_scheduling";

export type DraftStatus = "generated" | "edited" | "sent" | "discarded";

export type EventType =
  | "state_advanced"
  | "loop_marked_cold"
  | "loop_revived"
  | "email_drafted"
  | "email_sent"
  | "loop_created"
  | "thread_linked"
  | "thread_unlinked"
  | "actor_updated"
  | "note_added";

export const NEXT_ACTIONS: Record<StageState, string> = {
  new: "Email recruiter for availability",
  awaiting_candidate: "Waiting on candidate availability",
  awaiting_client: "Waiting on client to pick times",
  scheduled: "Interview scheduled",
  complete: "Complete",
  cold: "Stalled",
};

export interface Coordinator {
  id: string;
  name: string;
  email: string;
  created_at: string;
}

export interface CoordinatorOption {
  email: string;
  name: string | null;
}

export interface Suggestion {
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
}

export interface EmailDraft {
  id: string;
  suggestion_id: string;
  loop_id: string;
  coordinator_email: string;
  to_emails: string[];
  cc_emails: string[];
  subject: string;
  body: string;
  gmail_thread_id: string | null;
  is_forward: boolean;
  status: DraftStatus;
  pending_jit_data: Record<string, unknown>;
}

export interface SuggestionWithContext {
  suggestion: Suggestion;
  loop_title: string | null;
  loop_state: StageState | null;
  candidate_name: string | null;
  client_company: string | null;
  draft: EmailDraft | null;
  client_contact_name: string | null;
  client_contact_email: string | null;
  recruiter_name: string | null;
  recruiter_email: string | null;
  client_manager_name: string | null;
  client_manager_email: string | null;
}

export interface LoopSuggestionGroup {
  loop_id: string | null;
  loop_title: string | null;
  candidate_name: string | null;
  client_company: string | null;
  suggestions: SuggestionWithContext[];
  oldest_created_at: string;
}

export interface LoopFull {
  id: string;
  coordinator_id: string;
  title: string;
  notes: string | null;
  state: StageState;
  created_at: string;
  updated_at: string;
  coord_name: string;
  coord_email: string;
  candidate_name: string;
  candidate_notes: string | null;
  client_contact_name: string | null;
  client_contact_email: string | null;
  client_company: string | null;
  recruiter_name: string | null;
  recruiter_email: string | null;
  client_manager_name: string | null;
  client_manager_email: string | null;
}

export interface EmailThread {
  id: string;
  loop_id: string;
  gmail_thread_id: string;
  subject: string | null;
  linked_at: string;
}

export interface LoopEvent {
  id: string;
  loop_id: string;
  event_type: EventType;
  data: Record<string, unknown>;
  actor_email: string;
  occurred_at: string;
  // Optional suggestion context — populated when event.data.triggered_by
  // points to an agent_suggestions row. Lets the UI surface the agent's
  // reasoning inline alongside the event it caused.
  suggestion_id: string | null;
  suggestion_action: SuggestedAction | null;
  suggestion_summary: string | null;
  suggestion_reasoning: string | null;
}

export interface SuggestionForLoop {
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
  draft_id: string | null;
  draft_subject: string | null;
  draft_body: string | null;
  draft_status: DraftStatus | null;
  draft_to_emails: string[] | null;
  draft_cc_emails: string[] | null;
  draft_is_forward: boolean | null;
}
