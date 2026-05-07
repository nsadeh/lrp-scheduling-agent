import { Badge } from "@/components/ui/badge";
import type { DailyReviewItem } from "@/lib/queries/daily-review";

/**
 * Shared header block for review items: the agent's classification,
 * confidence, summary, and reasoning. Identical layout for create_loop
 * (Part 1) and draft_email/ask_coordinator (Part 2) so the reviewer
 * always knows where to find the "why."
 */
export function AgentJustification({ item }: { item: DailyReviewItem }) {
  return (
    <header className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">{item.action}</Badge>
        <Badge variant="secondary">{item.classification}</Badge>
        <Badge variant="outline">{item.status}</Badge>
        <span className="text-xs text-muted-foreground">
          confidence: {(item.confidence * 100).toFixed(0)}%
        </span>
      </div>

      {item.summary && (
        <div className="text-sm">
          <span className="text-xs uppercase tracking-wide text-muted-foreground mr-2">
            Summary
          </span>
          {item.summary}
        </div>
      )}

      {item.reasoning ? (
        <div className="border-l-2 border-primary/40 pl-3 py-1 bg-muted/30 rounded-r">
          <div className="text-[10px] uppercase tracking-widest text-primary font-semibold mb-0.5">
            Agent reasoning
          </div>
          <div className="text-xs whitespace-pre-wrap text-foreground">
            {item.reasoning}
          </div>
        </div>
      ) : (
        <div className="text-xs italic text-muted-foreground">
          No reasoning recorded for this suggestion.
        </div>
      )}
    </header>
  );
}
