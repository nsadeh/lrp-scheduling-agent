import type { EmailDraft } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

export function DraftPreview({ draft }: { draft: EmailDraft }) {
  return (
    <div className="border rounded p-3 bg-muted/30 space-y-1 text-sm">
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="bg-blue-50 text-blue-700">
          {draft.is_forward ? "Forward" : "Reply"}
        </Badge>
        <Badge variant="outline">{draft.status}</Badge>
      </div>
      <div className="text-xs text-muted-foreground">
        To: {draft.to_emails.join(", ")}
      </div>
      {draft.cc_emails.length > 0 && (
        <div className="text-xs text-muted-foreground">
          Cc: {draft.cc_emails.join(", ")}
        </div>
      )}
      <div className="font-medium">{draft.subject}</div>
      <pre className="whitespace-pre-wrap text-xs mt-2 p-2 bg-background rounded border max-h-40 overflow-y-auto">
        {draft.body}
      </pre>
    </div>
  );
}
