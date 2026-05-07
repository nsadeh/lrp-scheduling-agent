import type { DailyReviewItem } from "@/lib/queries/daily-review";

/**
 * Compact panel listing a loop's actor slots: candidate, client (with
 * company), recruiter, CM. Reused by both CreateLoopCard (as the
 * "resolved loop" half of the side-by-side comparison) and DecisionCard
 * (as the "who's on this loop" header for draft_email / ask_coordinator).
 *
 * Renders nullable fields as italic "(not set)" so the reviewer can
 * tell at a glance whether a draft is being targeted at a real
 * recipient or a placeholder.
 */
export function ActorList({
  title,
  rows,
}: {
  title: string;
  rows: { label: string; value: string | null; sub?: string | null }[];
}) {
  return (
    <div className="border rounded p-3 space-y-2">
      <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
        {title}
      </div>
      <dl className="space-y-1.5 text-sm">
        {rows.map((r) => (
          <div key={r.label}>
            <dt className="text-xs text-muted-foreground">{r.label}</dt>
            <dd className={r.value ? "" : "italic text-muted-foreground"}>
              {r.value ?? "(not set)"}
              {r.sub ? (
                <span className="text-xs text-muted-foreground"> — {r.sub}</span>
              ) : null}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** Build the standard "resolved loop" rows from a DailyReviewItem. */
export function loopActorRows(item: DailyReviewItem) {
  return [
    { label: "Candidate", value: item.candidate_name },
    {
      label: "Client",
      value: joinNameEmail(item.client_contact_name, item.client_contact_email),
      sub: item.client_company,
    },
    {
      label: "Recruiter",
      value: joinNameEmail(item.recruiter_name, item.recruiter_email),
    },
    {
      label: "CM",
      value: joinNameEmail(item.client_manager_name, item.client_manager_email),
    },
  ];
}

export function joinNameEmail(
  name: string | null | undefined,
  email: string | null | undefined
): string | null {
  if (!name && !email) return null;
  if (name && email) return `${name} <${email}>`;
  return name || email || null;
}
