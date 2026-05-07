import "server-only";

import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import type { DayReviewFile, ReviewEntry } from "./types";

/**
 * Local-filesystem storage for daily review entries. Writes are
 * deliberately segregated from Postgres to preserve the read-only
 * invariant of the prod DB.
 *
 * Path: services/debug-ui/.review-data/{slug}/{YYYY-MM-DD}.json
 *
 * Concurrency: write-after-read with an atomic `tmp + rename` to avoid
 * torn writes if two reviews are saved back-to-back. Single user / single
 * machine, so no locking needed.
 */

function dataDir(): string {
  return path.join(process.cwd(), ".review-data");
}

/** Sanitize an email into a path-safe slug. */
function emailSlug(email: string): string {
  return email.toLowerCase().replace(/[^a-z0-9]/g, "_");
}

function dayFilePath(coordinatorEmail: string, date: string): string {
  // Validate date shape so a malicious value can't escape the data dir.
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new Error(`invalid date: ${date}`);
  }
  return path.join(dataDir(), emailSlug(coordinatorEmail), `${date}.json`);
}

export async function readDayReview(
  coordinatorEmail: string,
  date: string
): Promise<DayReviewFile | null> {
  const file = dayFilePath(coordinatorEmail, date);
  try {
    const buf = await readFile(file, "utf8");
    return JSON.parse(buf) as DayReviewFile;
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return null;
    throw err;
  }
}

export async function writeReview(
  coordinatorEmail: string,
  date: string,
  entry: ReviewEntry
): Promise<DayReviewFile> {
  const existing = (await readDayReview(coordinatorEmail, date)) ?? {
    coordinator_email: coordinatorEmail,
    date,
    reviews: {},
    updated_at: new Date().toISOString(),
  };

  const next: DayReviewFile = {
    ...existing,
    reviews: {
      ...existing.reviews,
      [entry.suggestion_id]: entry,
    },
    updated_at: new Date().toISOString(),
  };

  const file = dayFilePath(coordinatorEmail, date);
  await mkdir(path.dirname(file), { recursive: true });

  // Atomic write: tmp + rename.
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(tmp, JSON.stringify(next, null, 2), "utf8");
  await rename(tmp, file);

  return next;
}

/**
 * Delete a single review entry by suggestion id. Used when the reviewer
 * wants to clear one item back to "unreviewed" without nuking the day.
 *
 * Returns the updated file (or null if the file didn't exist or the
 * entry wasn't there).
 */
export async function deleteReviewEntry(
  coordinatorEmail: string,
  date: string,
  suggestionId: string
): Promise<DayReviewFile | null> {
  const existing = await readDayReview(coordinatorEmail, date);
  if (!existing || !existing.reviews[suggestionId]) return existing;

  const { [suggestionId]: _removed, ...rest } = existing.reviews;
  void _removed;
  const next: DayReviewFile = {
    ...existing,
    reviews: rest,
    updated_at: new Date().toISOString(),
  };

  const file = dayFilePath(coordinatorEmail, date);
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(tmp, JSON.stringify(next, null, 2), "utf8");
  await rename(tmp, file);
  return next;
}

/**
 * Wipe the entire day's review file. Lets the user "redo" a report from
 * scratch: next visit to the flow page starts with a fresh form on
 * every item, and the report shows everything as unreviewed.
 *
 * Idempotent — silently no-ops if no file exists.
 */
export async function deleteDayReview(
  coordinatorEmail: string,
  date: string
): Promise<void> {
  const file = dayFilePath(coordinatorEmail, date);
  try {
    await unlink(file);
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return;
    throw err;
  }
}
