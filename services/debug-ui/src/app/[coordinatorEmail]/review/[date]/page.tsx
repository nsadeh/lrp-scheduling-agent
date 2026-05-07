import Link from "next/link";
import { notFound } from "next/navigation";
import { getDailyReviewItems } from "@/lib/queries/daily-review";
import { readDayReview } from "@/lib/reviews/storage";
import { formatETDate } from "@/lib/reviews/time";
import type { ReviewItemType } from "@/lib/reviews/types";
import { CreateLoopCard } from "@/components/review/create-loop-card";
import { DecisionCard } from "@/components/review/decision-card";
import { ThreadViewer } from "@/components/review/thread-viewer";
import { ReviewForm } from "@/components/review/review-form";
import { ResetDayButton } from "@/components/review/reset-buttons";
import { ItemIds } from "@/components/review/item-ids";
import { buttonVariants } from "@/components/ui/button";

/**
 * One-by-one review flow page.
 *
 * URL: /[coordinatorEmail]/review/[date]?i=N
 *
 * Items are sorted: Part 1 first (create_loop, by created_at), then Part 2
 * (draft_email + ask_coordinator interleaved by created_at). The reviewer
 * advances via Save & Next, which posts a server action and bumps `i`.
 */
export default async function ReviewFlowPage({
  params,
  searchParams,
}: {
  params: Promise<{ coordinatorEmail: string; date: string }>;
  searchParams: Promise<{ i?: string }>;
}) {
  const { coordinatorEmail: rawEmail, date } = await params;
  const coordinatorEmail = decodeURIComponent(rawEmail);
  const { i } = await searchParams;

  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) notFound();

  const [allItems, dayReview] = await Promise.all([
    getDailyReviewItems(coordinatorEmail, date),
    readDayReview(coordinatorEmail, date),
  ]);

  // Stable order: Part 1 (create_loop), then Part 2 (others).
  const part1 = allItems.filter((it) => it.action === "create_loop");
  const part2 = allItems.filter((it) => it.action !== "create_loop");
  const ordered = [...part1, ...part2];

  if (ordered.length === 0) {
    return (
      <div className="p-8 max-w-3xl mx-auto space-y-4">
        <BackLink coordinatorEmail={coordinatorEmail} />
        <h1 className="text-2xl font-semibold">No items to review</h1>
        <p className="text-muted-foreground">
          No create_loop, draft_email, or ask_coordinator suggestions were
          generated for {coordinatorEmail} on {formatETDate(date)} (ET).
        </p>
      </div>
    );
  }

  const idx = clampIndex(parseInt(i ?? "0", 10), ordered.length);
  const current = ordered[idx];
  const reviews = dayReview?.reviews ?? {};
  const reviewedCount = Object.keys(reviews).length;

  const encoded = encodeURIComponent(coordinatorEmail);
  const base = `/${encoded}/review/${date}`;
  const prevHref = idx > 0 ? `${base}?i=${idx - 1}` : null;
  const nextHref = idx < ordered.length - 1 ? `${base}?i=${idx + 1}` : null;
  const reportHref = `${base}/report`;

  // Part-aware header counters (e.g. "Part 1: 2 of 5")
  const isPart1 = current.action === "create_loop";
  const partLabel = isPart1 ? "Part 1: Loop Creation" : "Part 2: Decisions";
  const partIdx = isPart1
    ? part1.findIndex((it) => it.id === current.id)
    : part2.findIndex((it) => it.id === current.id);
  const partTotal = isPart1 ? part1.length : part2.length;

  return (
    // Scrollable region is full panel width so the scrollbar sticks to
    // the panel edge; the centered max-w-4xl column lives INSIDE it.
    <div className="overflow-y-auto h-full">
      <div className="p-6 max-w-4xl mx-auto space-y-5">
      <header className="flex items-start justify-between gap-4">
        <div>
          <BackLink coordinatorEmail={coordinatorEmail} />
          <h1 className="text-xl font-semibold mt-1">
            Reviewing: {formatETDate(date)} (ET)
          </h1>
          <p className="text-xs text-muted-foreground">
            {coordinatorEmail} · {ordered.length} item
            {ordered.length === 1 ? "" : "s"} · {reviewedCount} reviewed ·{" "}
            {ordered.length - reviewedCount} unreviewed
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ResetDayButton
            coordinatorEmail={coordinatorEmail}
            date={date}
            reviewedCount={reviewedCount}
          />
          <Link
            href={reportHref}
            className={buttonVariants({ variant: "outline" })}
          >
            View Report →
          </Link>
        </div>
      </header>

      <div className="space-y-2">
        <div className="text-xs text-muted-foreground">
          Item {idx + 1} of {ordered.length} · {partLabel} ({partIdx + 1} of{" "}
          {partTotal}) ·{" "}
          <span title="The agent_suggestions row was inserted in this UTC range">
            created {new Date(current.created_at).toLocaleString()}
          </span>
        </div>
        <ItemIds
          ids={[
            { label: "suggestion", value: current.id },
            { label: "loop", value: current.loop_id },
            { label: "thread", value: current.gmail_thread_id },
            { label: "message", value: current.gmail_message_id },
          ]}
        />
      </div>

      <div className="border rounded p-5 space-y-5 bg-card">
        {isPart1 ? (
          <CreateLoopCard item={current} />
        ) : (
          <DecisionCard item={current} />
        )}

        <details open className="border-t pt-4">
          <summary className="cursor-pointer text-sm font-medium select-none">
            Originating thread
          </summary>
          <div className="mt-3">
            <ThreadViewer
              coordinatorEmail={coordinatorEmail}
              threadId={current.gmail_thread_id}
              highlightMessageId={current.gmail_message_id}
              suggestionCreatedAt={current.created_at}
            />
          </div>
        </details>
      </div>

      {/*
       * `key={current.id}` is critical: navigation between items doesn't
       * unmount the form, so without a stable key React would keep the
       * useState values from the previous item. Re-keying on suggestion
       * id forces a fresh mount and re-runs the useState initializers
       * with the new `initial`.
       */}
      <ReviewForm
        key={current.id}
        coordinatorEmail={coordinatorEmail}
        date={date}
        suggestionId={current.id}
        itemType={current.action as ReviewItemType}
        initial={reviews[current.id] ?? null}
        prevHref={prevHref}
        nextHref={nextHref}
        reportHref={reportHref}
      />
      </div>
    </div>
  );
}

function BackLink({ coordinatorEmail }: { coordinatorEmail: string }) {
  return (
    <Link
      href={`/${encodeURIComponent(coordinatorEmail)}/review`}
      className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
    >
      ← All review days
    </Link>
  );
}

function clampIndex(i: number, len: number): number {
  if (Number.isNaN(i) || i < 0) return 0;
  if (i >= len) return len - 1;
  return i;
}
