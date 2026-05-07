import { getLoopFull } from "@/lib/queries/loops";
import { getThreadsForLoop } from "@/lib/queries/threads";
import { getSuggestionsForLoop } from "@/lib/queries/suggestions";
import { getEventsForLoop } from "@/lib/queries/events";
import { getGmailThread } from "@/lib/gmail/client";
import { StageBadge } from "./stage-badge";
import { ThreadList } from "./thread-list";
import { SuggestionHistory } from "./suggestion-history";
import { EventTimeline } from "./event-timeline";
import { Separator } from "@/components/ui/separator";
import type { GmailThread } from "@/lib/gmail/types";
import { NEXT_ACTIONS } from "@/lib/types";

export async function DetailPanel({
  loopId,
  coordinatorEmail,
}: {
  loopId: string;
  coordinatorEmail: string;
}) {
  const [loop, threads, suggestions, events] = await Promise.all([
    getLoopFull(loopId),
    getThreadsForLoop(loopId),
    getSuggestionsForLoop(loopId),
    getEventsForLoop(loopId),
  ]);

  if (!loop) {
    return (
      <div className="p-4 text-muted-foreground text-sm">Loop not found.</div>
    );
  }

  // Fetch Gmail threads in parallel — gracefully handle failures
  const gmailThreads = new Map<string, GmailThread>();
  const gmailResults = await Promise.allSettled(
    threads.map((t) => getGmailThread(coordinatorEmail, t.gmail_thread_id))
  );
  for (let i = 0; i < threads.length; i++) {
    const result = gmailResults[i];
    if (result.status === "fulfilled") {
      gmailThreads.set(threads[i].gmail_thread_id, result.value);
    }
  }

  return (
    <div className="p-4 space-y-5 overflow-y-auto h-full">
      {/* Loop header */}
      <div>
        <h2 className="text-lg font-semibold">{loop.title}</h2>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 mt-2 text-sm">
          <div>
            <span className="text-muted-foreground">Coordinator: </span>
            {loop.coord_name} ({loop.coord_email})
          </div>
          <div>
            <span className="text-muted-foreground">Candidate: </span>
            {loop.candidate_name}
          </div>
          <div>
            <span className="text-muted-foreground">Client: </span>
            {loop.client_contact_name ? (
              <>
                {loop.client_contact_name} ({loop.client_contact_email})
                {loop.client_company && ` — ${loop.client_company}`}
              </>
            ) : (
              <span className="italic text-muted-foreground">(not set)</span>
            )}
          </div>
          <div>
            <span className="text-muted-foreground">Recruiter: </span>
            {loop.recruiter_name ? (
              <>
                {loop.recruiter_name} ({loop.recruiter_email})
              </>
            ) : (
              <span className="italic text-muted-foreground">(not set)</span>
            )}
          </div>
          <div>
            <span className="text-muted-foreground">CM: </span>
            {loop.client_manager_name ? (
              <>
                {loop.client_manager_name} ({loop.client_manager_email})
              </>
            ) : (
              <span className="italic text-muted-foreground">(not set)</span>
            )}
          </div>
          <div className="text-xs text-muted-foreground col-span-2 mt-1">
            Created {new Date(loop.created_at).toLocaleString()} — Updated{" "}
            {new Date(loop.updated_at).toLocaleString()}
          </div>
        </div>
        {loop.notes && (
          <p className="text-sm text-muted-foreground mt-2">{loop.notes}</p>
        )}
      </div>

      <Separator />

      {/* State */}
      <div>
        <h3 className="text-sm font-semibold mb-2">State</h3>
        <div className="flex items-center gap-3 text-sm">
          <StageBadge state={loop.state} />
          <span className="text-xs text-muted-foreground">
            {NEXT_ACTIONS[loop.state]}
          </span>
        </div>
      </div>

      <Separator />

      {/* Threads with messages */}
      <div>
        <h3 className="text-sm font-semibold mb-2">
          Email Threads ({threads.length})
        </h3>
        <ThreadList
          threads={threads}
          gmailThreads={gmailThreads}
          suggestions={suggestions}
        />
      </div>

      <Separator />

      {/* All suggestions */}
      <div>
        <h3 className="text-sm font-semibold mb-2">
          Suggestion History ({suggestions.length})
        </h3>
        <SuggestionHistory suggestions={suggestions} />
      </div>

      <Separator />

      {/* Events */}
      <div>
        <h3 className="text-sm font-semibold mb-2">
          Event Timeline ({events.length})
        </h3>
        <EventTimeline events={events} />
      </div>
    </div>
  );
}
