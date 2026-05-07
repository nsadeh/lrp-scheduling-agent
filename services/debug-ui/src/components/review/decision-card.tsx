import { Badge } from "@/components/ui/badge";
import type { DailyReviewItem } from "@/lib/queries/daily-review";
import { AgentJustification } from "./agent-justification";
import { ActorList, loopActorRows } from "./actor-list";

/**
 * Renders the agent's "decision" output for review:
 *   - draft_email: who's it going to + the body
 *   - ask_coordinator: the question the agent surfaced
 *
 * Header (action / classification / summary / reasoning) is shared with
 * CreateLoopCard via AgentJustification so the reviewer always sees the
 * agent's "why" in the same spot. The loop's actor slots are rendered
 * just below — recipient routing depends on these, so they're essential
 * context when judging whether the draft is going to the right person.
 */
export function DecisionCard({ item }: { item: DailyReviewItem }) {
  const data = (item.action_data ?? {}) as Record<string, unknown>;
  const loopTitle = item.loop_title
    ? `${item.loop_title}${item.loop_state ? ` · ${item.loop_state}` : ""}`
    : "Loop context";

  return (
    <div className="space-y-3">
      <AgentJustification item={item} />

      <ActorList title={loopTitle} rows={loopActorRows(item)} />

      {item.action === "draft_email" && (
        <div className="border rounded p-3 space-y-2 text-sm">
          <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            <span className="text-muted-foreground">Subject:</span>
            <span>{item.draft_subject ?? "(none)"}</span>
            <span className="text-muted-foreground">To:</span>
            <span>
              {item.draft_to_emails && item.draft_to_emails.length > 0
                ? item.draft_to_emails.join(", ")
                : <em className="text-muted-foreground">(empty — JIT pending)</em>}
            </span>
            {item.draft_cc_emails && item.draft_cc_emails.length > 0 && (
              <>
                <span className="text-muted-foreground">Cc:</span>
                <span>{item.draft_cc_emails.join(", ")}</span>
              </>
            )}
            <span className="text-muted-foreground">Recipient type:</span>
            <span>
              <Badge variant="outline">
                {String(data.recipient_type ?? "(missing)")}
              </Badge>
            </span>
          </div>
          <pre className="whitespace-pre-wrap text-xs bg-muted rounded p-2 max-h-64 overflow-y-auto">
            {item.draft_body ?? "(no body)"}
          </pre>
        </div>
      )}

      {item.action === "ask_coordinator" && (
        <div className="border rounded p-3 text-sm">
          <div className="text-xs text-muted-foreground mb-1">
            Question to coordinator:
          </div>
          <div className="whitespace-pre-wrap">
            {String(data.question ?? data.body ?? "(no question)")}
          </div>
        </div>
      )}
    </div>
  );
}
