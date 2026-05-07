"use server";

import { revalidatePath } from "next/cache";
import { deleteDayReview, deleteReviewEntry, writeReview } from "./storage";
import type { ReviewEntry } from "./types";

function revalidateAll(coordinatorEmail: string, date: string): void {
  const encoded = encodeURIComponent(coordinatorEmail);
  revalidatePath(`/${encoded}/review/${date}`);
  revalidatePath(`/${encoded}/review/${date}/report`);
  revalidatePath(`/${encoded}/review`);
}

/**
 * Server action: persist a single review entry and trigger a re-render
 * of the review pages so the new state shows up in the UI.
 *
 * Called from the client form via React's `<form action={...}>` or
 * a direct invocation. Server-only because `writeReview` touches the
 * filesystem.
 */
export async function saveReviewAction(input: {
  coordinatorEmail: string;
  date: string;
  entry: ReviewEntry;
}): Promise<void> {
  await writeReview(input.coordinatorEmail, input.date, input.entry);
  revalidateAll(input.coordinatorEmail, input.date);
}

/**
 * Server action: clear a single item's review (back to "unreviewed").
 */
export async function resetReviewItemAction(input: {
  coordinatorEmail: string;
  date: string;
  suggestionId: string;
}): Promise<void> {
  await deleteReviewEntry(
    input.coordinatorEmail,
    input.date,
    input.suggestionId
  );
  revalidateAll(input.coordinatorEmail, input.date);
}

/**
 * Server action: wipe the day's review file entirely. Use this when the
 * reviewer wants to redo the entire day's report from scratch — every
 * item resets to unreviewed.
 */
export async function resetDayReviewAction(input: {
  coordinatorEmail: string;
  date: string;
}): Promise<void> {
  await deleteDayReview(input.coordinatorEmail, input.date);
  revalidateAll(input.coordinatorEmail, input.date);
}
