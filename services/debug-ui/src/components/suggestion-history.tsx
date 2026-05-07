import { Badge } from "@/components/ui/badge";
import type { SuggestionForLoop, SuggestionStatus } from "@/lib/types";

const statusColors: Record<SuggestionStatus, string> = {
  pending: "bg-yellow-100 text-yellow-800",
  accepted: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
  expired: "bg-gray-100 text-gray-500",
  auto_applied: "bg-blue-100 text-blue-800",
  superseded: "bg-slate-100 text-slate-500",
};

const statusIcons: Record<SuggestionStatus, string> = {
  pending: "...",
  accepted: "ok",
  rejected: "x",
  expired: "exp",
  auto_applied: "auto",
  superseded: "sup",
};

const recipientColors: Record<string, string> = {
  recruiter: "bg-blue-50 text-blue-700",
  client: "bg-green-50 text-green-700",
  internal: "bg-slate-100 text-slate-700",
};

/**
 * Compact draft preview inline in the timeline. Prefers the joined draft
 * (resolved recipients) and falls back to action_data (LLM intent + body)
 * when the draft is missing — superseded/expired suggestions often have
 * no draft row anymore.
 */
function InlineDraftPreview({ s }: { s: SuggestionForLoop }) {
  const actionData = s.action_data || {};
  const recipientType = (actionData.recipient_type as string) || null;
  const body =
    s.draft_body ||
    (typeof actionData.body === "string" ? actionData.body : "");
  const toEmails = s.draft_to_emails || [];

  // Nothing useful to show — render nothing rather than an empty box.
  if (!body && toEmails.length === 0 && !recipientType) return null;

  return (
    <div className="mt-1 border rounded p-2 bg-muted/30 space-y-1">
      <div className="flex items-center gap-2 flex-wrap">
        {recipientType && (
          <Badge
            variant="outline"
            className={recipientColors[recipientType] || ""}
          >
            → {recipientType}
          </Badge>
        )}
        {toEmails.length > 0 && (
          <span className="text-muted-foreground text-[11px]">
            To: {toEmails.join(", ")}
          </span>
        )}
        {s.draft_subject && (
          <span className="text-foreground font-medium text-[11px] truncate">
            {s.draft_subject}
          </span>
        )}
      </div>
      {body && (
        <pre className="whitespace-pre-wrap text-[11px] p-2 bg-background rounded border max-h-32 overflow-y-auto">
          {body}
        </pre>
      )}
    </div>
  );
}

export function SuggestionHistory({
  suggestions,
}: {
  suggestions: SuggestionForLoop[];
}) {
  if (suggestions.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">No suggestions for this loop.</p>
    );
  }

  return (
    <div className="space-y-2">
      {suggestions.map((s) => (
        <div key={s.id} className="flex items-start gap-2 text-xs">
          <Badge
            variant="outline"
            className={`shrink-0 ${statusColors[s.status as SuggestionStatus] || ""}`}
          >
            {statusIcons[s.status as SuggestionStatus] || s.status}
          </Badge>
          <div className="min-w-0 flex-1">
            <span className="font-medium">
              {s.action.replace(/_/g, " ")}
            </span>
            <span className="text-muted-foreground ml-1">{s.summary}</span>
            {s.action === "draft_email" && <InlineDraftPreview s={s} />}
            <div className="text-muted-foreground mt-1">
              {new Date(s.created_at).toLocaleString()}
              {s.resolved_at &&
                ` — resolved ${new Date(s.resolved_at).toLocaleString()}`}
              {s.resolved_by && ` by ${s.resolved_by}`}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
