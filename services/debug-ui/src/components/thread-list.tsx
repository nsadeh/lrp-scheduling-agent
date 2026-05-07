import type { EmailThread, SuggestionForLoop } from "@/lib/types";
import type { GmailThread } from "@/lib/gmail/types";
import { MessageCard } from "./message-card";
import { Badge } from "@/components/ui/badge";

function SuggestionsForMessage({
  messageId,
  suggestions,
}: {
  messageId: string;
  suggestions: SuggestionForLoop[];
}) {
  const matched = suggestions.filter((s) => s.gmail_message_id === messageId);
  if (matched.length === 0) return null;

  return (
    <div className="ml-4 border-l-2 border-blue-200 pl-3 py-1 space-y-1">
      <div className="text-xs font-medium text-blue-700">
        Agent suggestions on this message:
      </div>
      {matched.map((s) => (
        <div key={s.id} className="flex items-center gap-1 text-xs">
          <Badge variant="outline" className="text-[10px] px-1.5 py-0">
            {s.status}
          </Badge>
          <span>{s.action.replace(/_/g, " ")}</span>
          <span className="text-muted-foreground">{s.summary}</span>
        </div>
      ))}
    </div>
  );
}

export function ThreadList({
  threads,
  gmailThreads,
  suggestions,
}: {
  threads: EmailThread[];
  gmailThreads: Map<string, GmailThread>;
  suggestions: SuggestionForLoop[];
}) {
  if (threads.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No email threads linked to this loop.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {threads.map((thread) => {
        const gmailThread = gmailThreads.get(thread.gmail_thread_id);

        return (
          <div key={thread.id} className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">
                {thread.subject || "No subject"}
              </span>
              <span className="text-xs text-muted-foreground">
                {thread.gmail_thread_id}
              </span>
            </div>
            {gmailThread ? (
              <div className="space-y-2">
                {gmailThread.messages.map((msg) => (
                  <div key={msg.id}>
                    <MessageCard message={msg} />
                    <SuggestionsForMessage
                      messageId={msg.id}
                      suggestions={suggestions}
                    />
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground italic">
                Could not fetch Gmail thread messages.
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
