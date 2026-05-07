import { query } from "@/lib/db";
import type { EmailThread } from "@/lib/types";

export async function getThreadsForLoop(
  loopId: string
): Promise<EmailThread[]> {
  return query<EmailThread>(
    `SELECT id, loop_id, gmail_thread_id, subject, linked_at
     FROM loop_email_threads
     WHERE loop_id = $1
     ORDER BY linked_at`,
    [loopId]
  );
}

export async function getThreadsForLoops(
  loopIds: string[]
): Promise<EmailThread[]> {
  return query<EmailThread>(
    `SELECT id, loop_id, gmail_thread_id, subject, linked_at
     FROM loop_email_threads
     WHERE loop_id = ANY($1::text[])
     ORDER BY loop_id, linked_at`,
    [loopIds]
  );
}
