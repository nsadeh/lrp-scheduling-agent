import type { GmailMessage } from "@/lib/gmail/types";

function formatAddr(addr: { name: string | null; email: string }) {
  return addr.name ? `${addr.name} <${addr.email}>` : addr.email;
}

export function MessageCard({ message }: { message: GmailMessage }) {
  return (
    <div className="border rounded p-3 space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{formatAddr(message.from)}</span>
        <span className="text-xs text-muted-foreground">
          {new Date(message.date).toLocaleString()}
        </span>
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
