import { google } from "googleapis";
import { queryOne } from "@/lib/db";
import { fernetDecrypt } from "./fernet";
import { parseMessage } from "./parse-message";
import type { GmailThread } from "./types";

interface TokenRow {
  refresh_token_encrypted: Buffer;
}

async function getRefreshToken(coordinatorEmail: string): Promise<string> {
  const row = await queryOne<TokenRow>(
    `SELECT refresh_token_encrypted FROM gmail_tokens WHERE user_email = $1`,
    [coordinatorEmail]
  );
  if (!row) {
    throw new Error(`No Gmail token for ${coordinatorEmail}`);
  }

  const key = process.env.GMAIL_TOKEN_ENCRYPTION_KEY;
  if (!key) {
    throw new Error("GMAIL_TOKEN_ENCRYPTION_KEY not set");
  }

  return fernetDecrypt(row.refresh_token_encrypted, key);
}

function buildAuth(refreshToken: string) {
  const oauth2 = new google.auth.OAuth2(
    process.env.GOOGLE_OAUTH_CLIENT_ID,
    process.env.GOOGLE_OAUTH_CLIENT_SECRET
  );
  oauth2.setCredentials({ refresh_token: refreshToken });
  return oauth2;
}

export async function getGmailThread(
  coordinatorEmail: string,
  threadId: string
): Promise<GmailThread> {
  const refreshToken = await getRefreshToken(coordinatorEmail);
  const auth = buildAuth(refreshToken);
  const gmail = google.gmail({ version: "v1", auth });

  const res = await gmail.users.threads.get({
    userId: "me",
    id: threadId,
    format: "full",
  });

  const messages = (
    (res.data.messages as Record<string, unknown>[]) || []
  ).map(parseMessage);
  messages.sort(
    (a, b) => new Date(a.date).getTime() - new Date(b.date).getTime()
  );

  return { id: res.data.id!, messages };
}
