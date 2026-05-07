export interface EmailAddress {
  name: string | null;
  email: string;
}

export interface GmailMessage {
  id: string;
  threadId: string;
  subject: string;
  from: EmailAddress;
  to: EmailAddress[];
  cc: EmailAddress[];
  date: string;
  bodyText: string;
  snippet: string;
  labelIds: string[];
}

export interface GmailThread {
  id: string;
  messages: GmailMessage[];
}
