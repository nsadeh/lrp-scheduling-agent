import { query } from "@/lib/db";
import type { LoopEvent } from "@/lib/types";

/**
 * Fetch loop_events with the originating suggestion's reasoning joined in.
 *
 * Two join paths, COALESCEd:
 *
 * 1. **triggered_by path** — `state_advanced` and `loop_marked_cold` events
 *    store `data.triggered_by = "auto:sug_xxx"`. Regex-extract the sug_id and
 *    look up the suggestion directly.
 *
 * 2. **gmail_thread path** — `loop_created` events have NO triggered_by (the
 *    backend doesn't write one for them) AND the originating `create_loop`
 *    suggestion has `loop_id = NULL` (suggested before the loop existed). So
 *    we link via the gmail_thread_id of the loop's linked threads — pick the
 *    earliest matching `create_loop` suggestion. LATERAL because the inner
 *    query references e.loop_id from the outer row.
 *
 * Manual events (`triggered_by = "manual:user@x.com"`) and events without any
 * suggestion linkage (`thread_linked`, `email_sent`, ...) get null fields.
 */
export async function getEventsForLoop(loopId: string): Promise<LoopEvent[]> {
  return query<LoopEvent>(
    `SELECT
       e.id, e.loop_id, e.event_type, e.data, e.actor_email, e.occurred_at,
       COALESCE(s_trig.id, s_create.id)               AS suggestion_id,
       COALESCE(s_trig.action, s_create.action)       AS suggestion_action,
       COALESCE(s_trig.summary, s_create.summary)     AS suggestion_summary,
       COALESCE(s_trig.reasoning, s_create.reasoning) AS suggestion_reasoning
     FROM loop_events e
     LEFT JOIN agent_suggestions s_trig
       ON s_trig.id = SUBSTRING(e.data->>'triggered_by' FROM 'sug_[A-Za-z0-9_-]+')
     LEFT JOIN LATERAL (
       SELECT s.id, s.action, s.summary, s.reasoning
       FROM agent_suggestions s
       JOIN loop_email_threads let
         ON let.gmail_thread_id = s.gmail_thread_id AND let.loop_id = e.loop_id
       WHERE e.event_type = 'loop_created'
         AND s.action = 'create_loop'
       ORDER BY s.created_at
       LIMIT 1
     ) s_create ON true
     WHERE e.loop_id = $1
     ORDER BY e.occurred_at`,
    [loopId]
  );
}
