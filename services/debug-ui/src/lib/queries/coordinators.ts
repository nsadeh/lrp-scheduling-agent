import { query } from "@/lib/db";
import type { CoordinatorOption } from "@/lib/types";

export async function getCoordinatorEmails(): Promise<CoordinatorOption[]> {
  return query<CoordinatorOption>(
    `SELECT gt.user_email AS email, c.name
     FROM gmail_tokens gt
     LEFT JOIN coordinators c ON c.email = gt.user_email
     ORDER BY gt.user_email`
  );
}

export async function getCoordinatorIdByEmail(
  email: string
): Promise<string | null> {
  const rows = await query<{ id: string }>(
    `SELECT id FROM coordinators WHERE email = $1`,
    [email]
  );
  return rows[0]?.id ?? null;
}
