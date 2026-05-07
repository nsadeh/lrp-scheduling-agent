import { getGmailThread } from "@/lib/gmail/client";
import { MessageCard } from "@/components/message-card";

/**
 * Renders a Gmail thread inline alongside an item being reviewed. The
 * fetch is best-effort: if Gmail OAuth fails we surface a tasteful error
 * rather than crashing the whole flow.
 *
 * When `suggestionCreatedAt` is supplied we render an unmissable banner
 * at the moment the suggestion was generated. Messages dated at-or-before
 * the cutoff = "what the agent had to work with." Messages after =
 * "what the human did instead / what happened next." Both sets stay
 * visible so the reviewer can compare the agent's draft against the
 * human's actual reply when grading incorrect calls.
 */
export async function ThreadViewer({
  coordinatorEmail,
  threadId,
  highlightMessageId,
  suggestionCreatedAt,
}: {
  coordinatorEmail: string;
  threadId: string;
  /** The agent_suggestions.gmail_message_id that triggered the suggestion. */
  highlightMessageId?: string | null;
  /**
   * ISO timestamp — when the agent_suggestions row was inserted. Drives
   * the placement of the "before / after" divider banner.
   */
  suggestionCreatedAt?: string | null;
}) {
  let thread;
  try {
    thread = await getGmailThread(coordinatorEmail, threadId);
  } catch (err) {
    return (
      <div className="border rounded p-3 text-xs text-muted-foreground italic">
        Could not load thread {threadId}:{" "}
        {err instanceof Error ? err.message : String(err)}
      </div>
    );
  }

  if (!thread || thread.messages.length === 0) {
    return (
      <div className="border rounded p-3 text-xs text-muted-foreground italic">
        Thread is empty.
      </div>
    );
  }

  // Sort defensively (Gmail typically returns chronological, but don't trust).
  const sorted = [...thread.messages].sort(
    (a, b) => new Date(a.date).getTime() - new Date(b.date).getTime()
  );

  const cutoffMs = suggestionCreatedAt
    ? new Date(suggestionCreatedAt).getTime()
    : null;

  // splitIndex = index of the first message dated AFTER the cutoff.
  // i.e. messages[0..splitIndex) were available to the agent;
  // messages[splitIndex..] arrived later.
  const splitIndex =
    cutoffMs == null
      ? sorted.length
      : sorted.findIndex((m) => new Date(m.date).getTime() > cutoffMs);
  const effectiveSplit = splitIndex === -1 ? sorted.length : splitIndex;

  const beforeCount = effectiveSplit;
  const afterCount = sorted.length - effectiveSplit;

  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">
        Thread: {sorted[0].subject} ({sorted.length} message
        {sorted.length === 1 ? "" : "s"}
        {cutoffMs != null && (
          <>
            {" · "}
            <span>{beforeCount} before suggestion</span>
            {" · "}
            <span>{afterCount} after</span>
          </>
        )}
        )
      </div>

      {sorted.map((m, i) => (
        <div key={m.id}>
          {i === effectiveSplit && cutoffMs != null && (
            <SuggestionDivider createdAt={suggestionCreatedAt!} />
          )}
          <div
            className={
              m.id === highlightMessageId
                ? "ring-2 ring-primary/40 rounded"
                : undefined
            }
          >
            <MessageCard message={m} />
            {m.id === highlightMessageId && (
              <div className="text-[10px] uppercase tracking-wide text-primary mt-0.5">
                ↑ Trigger message for this suggestion
              </div>
            )}
          </div>
        </div>
      ))}

      {/* Edge case: no "after" messages — render a tail banner so the
          reviewer still sees the cutoff and knows nothing followed. */}
      {cutoffMs != null && afterCount === 0 && beforeCount > 0 && (
        <SuggestionDivider createdAt={suggestionCreatedAt!} trailing />
      )}
    </div>
  );
}

/**
 * The "agent acted here" banner. Designed to be impossible to miss
 * when scanning the thread: brand-purple double rule, centered chip,
 * plain-English label.
 */
function SuggestionDivider({
  createdAt,
  trailing,
}: {
  createdAt: string;
  trailing?: boolean;
}) {
  const formatted = new Date(createdAt).toLocaleString();
  return (
    <div className="my-4">
      <div
        className="relative flex items-center justify-center"
        style={{ minHeight: "1.5rem" }}
      >
        <div
          className="absolute inset-x-0 top-1/2 -translate-y-1/2"
          style={{
            borderTop: "2px solid var(--color-brand-purple, #5b099b)",
            borderBottom: "2px solid var(--color-brand-purple, #5b099b)",
            height: "6px",
          }}
        />
        <div
          className="relative px-3 py-1 rounded-full text-[11px] font-semibold uppercase tracking-widest text-white shadow-sm"
          style={{ background: "var(--color-brand-purple, #5b099b)" }}
        >
          ⚡ Suggestion created · {formatted}
        </div>
      </div>
      <div className="text-[10px] uppercase tracking-wider text-center text-muted-foreground mt-1">
        {trailing
          ? "Nothing followed — the human hasn't replied yet"
          : "↑ What the agent saw   ·   ↓ What happened next"}
      </div>
    </div>
  );
}
