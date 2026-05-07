import type { EmailAddress, GmailMessage } from "./types";

function getHeader(
  headers: Array<{ name: string; value: string }>,
  name: string
): string {
  const h = headers.find(
    (h) => h.name.toLowerCase() === name.toLowerCase()
  );
  return h?.value ?? "";
}

function parseEmailAddress(raw: string): EmailAddress {
  const match = raw.match(/^(.+?)\s*<([^>]+)>$/);
  if (match) {
    return { name: match[1].trim().replace(/^"|"$/g, ""), email: match[2] };
  }
  return { name: null, email: raw.trim() };
}

function parseEmailAddressList(raw: string): EmailAddress[] {
  if (!raw) return [];
  return raw
    .split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/)
    .map((s) => parseEmailAddress(s.trim()))
    .filter((a) => a.email);
}

function stripHtml(html: string): string {
  let text = html;
  text = text.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "");
  text = text.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "");
  text = text.replace(/<br\s*\/?>/gi, "\n");
  text = text.replace(/<\/(?:p|div|tr|li|h[1-6])>/gi, "\n");
  text = text.replace(/<[^>]+>/g, "");
  text = text.replace(/&nbsp;/g, " ");
  text = text.replace(/&amp;/g, "&");
  text = text.replace(/&lt;/g, "<");
  text = text.replace(/&gt;/g, ">");
  text = text.replace(/&quot;/g, '"');
  text = text.replace(/&#39;/g, "'");
  text = text.replace(/\n{3,}/g, "\n\n");
  return text.trim();
}

function extractBodyText(payload: Record<string, unknown>): string {
  const mimeType = (payload.mimeType as string) || "";
  const body = payload.body as Record<string, unknown> | undefined;
  const parts = payload.parts as Record<string, unknown>[] | undefined;

  if (mimeType === "text/plain" && body?.data) {
    return Buffer.from(body.data as string, "base64url").toString("utf-8");
  }

  if (parts) {
    for (const part of parts) {
      if (
        (part.mimeType as string) === "text/plain" &&
        (part.body as Record<string, unknown>)?.data
      ) {
        return Buffer.from(
          (part.body as Record<string, unknown>).data as string,
          "base64url"
        ).toString("utf-8");
      }
    }

    for (const part of parts) {
      if ((part.mimeType as string)?.startsWith("multipart/")) {
        const result = extractBodyText(part);
        if (result) return result;
      }
    }

    for (const part of parts) {
      if (
        (part.mimeType as string) === "text/html" &&
        (part.body as Record<string, unknown>)?.data
      ) {
        const html = Buffer.from(
          (part.body as Record<string, unknown>).data as string,
          "base64url"
        ).toString("utf-8");
        return stripHtml(html);
      }
    }
  }

  if (body?.data) {
    const data = Buffer.from(body.data as string, "base64url").toString(
      "utf-8"
    );
    if (mimeType.includes("text/html")) {
      return stripHtml(data);
    }
    return data;
  }

  return "";
}

export function parseMessage(
  raw: Record<string, unknown>
): GmailMessage {
  const payload = raw.payload as Record<string, unknown> | undefined;
  const headers =
    (payload?.headers as Array<{ name: string; value: string }>) || [];

  const dateStr = getHeader(headers, "Date");
  let date: string;
  try {
    date = new Date(dateStr).toISOString();
  } catch {
    date = new Date().toISOString();
  }

  return {
    id: raw.id as string,
    threadId: raw.threadId as string,
    subject: getHeader(headers, "Subject"),
    from: parseEmailAddress(getHeader(headers, "From")),
    to: parseEmailAddressList(getHeader(headers, "To")),
    cc: parseEmailAddressList(getHeader(headers, "Cc")),
    date,
    bodyText: payload ? extractBodyText(payload) : "",
    snippet: (raw.snippet as string) || "",
    labelIds: (raw.labelIds as string[]) || [],
  };
}
