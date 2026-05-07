/**
 * Types for the daily-review feature.
 *
 * Reviews live in local JSON files (one per coordinator-day) under
 * `services/debug-ui/.review-data/`. We deliberately keep these types
 * separate from the database-backed types in `@/lib/types` because they
 * represent reviewer annotations, not agent state.
 */

export type ReviewItemType = "create_loop" | "draft_email" | "ask_coordinator";

export interface ReviewEntry {
  /** The agent_suggestions.id this review covers. */
  suggestion_id: string;
  /** The action of the underlying suggestion. */
  item_type: ReviewItemType;
  /** Reviewer's verdict. Defaults to true (correct). */
  correct: boolean;
  /** Free-text description of what the agent got wrong. Empty when correct. */
  what_was_wrong: string;
  /** Reviewer's hypothesis for why the agent decided that way. Empty when correct. */
  why_incorrect: string;
  /** ISO timestamp of last save. */
  reviewed_at: string;
}

export interface DayReviewFile {
  coordinator_email: string;
  /** YYYY-MM-DD in Eastern time. */
  date: string;
  /** Keyed by suggestion_id so re-saves overwrite cleanly. */
  reviews: Record<string, ReviewEntry>;
  updated_at: string;
}
