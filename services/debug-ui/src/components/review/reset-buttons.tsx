"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  resetDayReviewAction,
  resetReviewItemAction,
} from "@/lib/reviews/actions";

/**
 * Day-level redo: wipes the JSON file so every item resets to
 * "unreviewed". Used in the flow page header when the reviewer wants to
 * start the report over.
 */
export function ResetDayButton({
  coordinatorEmail,
  date,
  reviewedCount,
}: {
  coordinatorEmail: string;
  date: string;
  reviewedCount: number;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  if (reviewedCount === 0) return null;

  return (
    <Button
      variant="outline"
      size="sm"
      disabled={isPending}
      onClick={() => {
        const ok = window.confirm(
          `Reset all ${reviewedCount} reviews for ${date}? This permanently deletes the .review-data file. The agent's suggestions are not touched.`
        );
        if (!ok) return;
        startTransition(async () => {
          await resetDayReviewAction({ coordinatorEmail, date });
          router.replace(
            `/${encodeURIComponent(coordinatorEmail)}/review/${date}?i=0`
          );
        });
      }}
    >
      {isPending ? "Resetting…" : "Reset day"}
    </Button>
  );
}

/**
 * Single-item redo: clears just this suggestion's review entry, leaving
 * the rest of the day intact. Used inside the form, only visible when
 * the current item already has a saved review.
 */
export function ResetItemButton({
  coordinatorEmail,
  date,
  suggestionId,
}: {
  coordinatorEmail: string;
  date: string;
  suggestionId: string;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  return (
    <Button
      variant="ghost"
      size="sm"
      disabled={isPending}
      onClick={() => {
        startTransition(async () => {
          await resetReviewItemAction({
            coordinatorEmail,
            date,
            suggestionId,
          });
          router.refresh();
        });
      }}
    >
      {isPending ? "Clearing…" : "Clear my answer"}
    </Button>
  );
}
