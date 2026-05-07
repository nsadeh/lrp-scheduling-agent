import type { DailyReviewItem } from "@/lib/queries/daily-review";
import type { ReviewEntry } from "@/lib/reviews/types";

/**
 * Per-item card for incorrect agent decisions. Brand-purple left border
 * for emphasis. Each card carries data-print-keep-together so it doesn't
 * split across pages.
 */
export function ReportIncorrect({
  pairs,
}: {
  pairs: { item: DailyReviewItem; review: ReviewEntry }[];
}) {
  if (pairs.length === 0) {
    return (
      <div className="text-xs italic text-muted-foreground">
        No incorrect items flagged.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {pairs.map(({ item, review }) => (
        <div
          key={item.id}
          data-print-keep-together
          className="rounded-r-md p-3"
          style={{
            background: "var(--color-brand-grey-light)",
            borderLeft: "4px solid var(--color-brand-purple)",
          }}
        >
          <div className="flex items-baseline justify-between gap-3">
            <div className="font-medium text-sm">
              {item.action === "create_loop" ? (
                <>
                  Loop creation:{" "}
                  {item.candidate_name ??
                    extracted(item, "candidate_name") ??
                    "(no candidate)"}{" "}
                  →{" "}
                  {item.client_company ??
                    extracted(item, "client_company") ??
                    "(no client)"}
                </>
              ) : (
                <>
                  {item.action}:{" "}
                  <span className="font-normal">
                    {item.loop_title ?? "(no loop)"}
                  </span>
                </>
              )}
            </div>
            <span
              className="text-[10px] font-semibold uppercase tracking-wide"
              style={{ color: "var(--color-brand-purple)" }}
            >
              Incorrect
            </span>
          </div>

          <div className="text-xs text-muted-foreground mt-1">
            {item.summary}
          </div>

          {review.what_was_wrong && (
            <div className="mt-2 text-sm">
              <span
                className="font-semibold text-xs uppercase tracking-wide"
                style={{ color: "var(--color-brand-purple)" }}
              >
                What was wrong:{" "}
              </span>
              <span className="whitespace-pre-wrap">
                {review.what_was_wrong}
              </span>
            </div>
          )}

          {review.why_incorrect && (
            <div className="mt-1 text-sm">
              <span
                className="font-semibold text-xs uppercase tracking-wide"
                style={{ color: "var(--color-brand-purple)" }}
              >
                Why I think:{" "}
              </span>
              <span className="whitespace-pre-wrap">
                {review.why_incorrect}
              </span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function extracted(item: DailyReviewItem, key: string): string | null {
  const data = (item.action_data ?? {}) as Record<string, string | null>;
  return data[key] ?? null;
}
