import type { LoopEvent } from "@/lib/types";
import { JsonViewer } from "./json-viewer";

const eventLabels: Record<string, string> = {
  state_advanced: "State Advanced",
  loop_marked_cold: "Loop Marked Cold",
  loop_revived: "Loop Revived",
  email_drafted: "Email Drafted",
  email_sent: "Email Sent",
  loop_created: "Loop Created",
  thread_linked: "Thread Linked",
  thread_unlinked: "Thread Unlinked",
  actor_updated: "Actor Updated",
  note_added: "Note Added",
};

export function EventTimeline({ events }: { events: LoopEvent[] }) {
  if (events.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">No events for this loop.</p>
    );
  }

  return (
    <div className="space-y-2">
      {events.map((e) => (
        <div key={e.id} className="flex gap-3 text-xs">
          <span className="text-muted-foreground shrink-0 w-36">
            {new Date(e.occurred_at).toLocaleString()}
          </span>
          <div className="min-w-0 flex-1">
            <span className="font-medium">
              {eventLabels[e.event_type] || e.event_type}
            </span>
            <span className="text-muted-foreground ml-1">
              by {e.actor_email}
            </span>
            {e.suggestion_id && (
              <div className="mt-1 border-l-2 border-blue-200 pl-2 space-y-0.5">
                {e.suggestion_summary && (
                  <div className="text-foreground">
                    <span className="text-muted-foreground">Suggestion: </span>
                    {e.suggestion_summary}
                  </div>
                )}
                {e.suggestion_reasoning && (
                  <div className="text-muted-foreground italic whitespace-pre-wrap">
                    {e.suggestion_reasoning}
                  </div>
                )}
              </div>
            )}
            <JsonViewer data={e.data} label="Data" />
          </div>
        </div>
      ))}
    </div>
  );
}
