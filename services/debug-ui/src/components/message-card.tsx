import type { GmailMessage } from "@/lib/gmail/types";
import { Badge } from "@/components/ui/badge";

function formatAddr(addr: { name: string | null; email: string }) {
  return addr.name ? `${addr.name} <${addr.email}>` : addr.email;
}

export function MessageCard({
  message,
  threadLabel,
}: {
  message: GmailMessage;
  threadLabel?: { subject: string; gmailThreadId: string };
}) {
  return (
    <div className="border rounded p-3 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium truncate">
          {formatAddr(message.from)}
        </span>
        <div className="flex items-center gap-2 shrink-0">
          {threadLabel && (
            <Badge
              variant="outline"
              className="text-[10px] font-normal max-w-[16rem] truncate"
              title={`${threadLabel.subject} (${threadLabel.gmailThreadId})`}
            >
              <span className="truncate">{threadLabel.subject || "(no subject)"}</span>
              <span className="ml-1 text-muted-foreground">
                {threadLabel.gmailThreadId.slice(-6)}
              </span>
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">
            {new Date(message.date).toLocaleString()}
          </span>
        </div>
      </div>
      <div className="text-xs text-muted-foreground">
        To: {message.to.map(formatAddr).join(", ")}
      </div>
      {message.cc.length > 0 && (
        <div className="text-xs text-muted-foreground">
          Cc: {message.cc.map(formatAddr).join(", ")}
        </div>
      )}
      <pre className="whitespace-pre-wrap text-xs mt-2 p-2 bg-muted rounded max-h-48 overflow-y-auto">
        {message.bodyText || message.snippet}
      </pre>
    </div>
  );
}
