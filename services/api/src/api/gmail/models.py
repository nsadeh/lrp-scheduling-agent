"""Pydantic models for parsed Gmail messages, threads, and drafts."""

from __future__ import annotations

import base64
import email.utils
import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class EmailAddress(BaseModel):
    """Parsed email address with optional display name."""

    name: str | None = None
    email: str


class Message(BaseModel):
    """Parsed Gmail message."""

    id: str
    thread_id: str
    subject: str
    from_: EmailAddress = Field(alias="from")
    to: list[EmailAddress]
    cc: list[EmailAddress] = []
    date: datetime
    body_text: str
    snippet: str = ""
    label_ids: list[str] = []
    message_id_header: str | None = None

    model_config = {"populate_by_name": True}


class Thread(BaseModel):
    """A Gmail thread with all its messages, chronologically ordered."""

    id: str
    messages: list[Message]


class Draft(BaseModel):
    """A Gmail draft."""

    id: str
    message: Message


class HistoryRecord(BaseModel):
    """A single history record from Gmail history.list."""

    messages_added: list[str] = []  # message IDs
    messages_deleted: list[str] = []  # message IDs


# ---------------------------------------------------------------------------
# Parsing helpers — convert raw Gmail API responses to our models
# ---------------------------------------------------------------------------

_HEADER_ENCODING_RE = re.compile(r"=\?[^?]+\?[BbQq]\?[^?]+\?=")


def _decode_header(value: str) -> str:
    """Decode RFC 2047 encoded header values."""
    if not _HEADER_ENCODING_RE.search(value):
        return value
    decoded_parts = email.header.decode_header(value)
    parts = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return "".join(parts)


import email.header  # noqa: E402


def parse_email_address(raw: str) -> EmailAddress:
    """Parse 'Display Name <addr@example.com>' into EmailAddress."""
    name, addr = email.utils.parseaddr(raw)
    return EmailAddress(name=name or None, email=addr)


def parse_email_address_list(raw: str) -> list[EmailAddress]:
    """Parse a comma-separated list of email addresses."""
    if not raw:
        return []
    addresses = email.utils.getaddresses([raw])
    return [EmailAddress(name=name or None, email=addr) for name, addr in addresses if addr]


def _get_header(headers: list[dict], name: str) -> str:
    """Extract a header value by name from Gmail's headers list."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _extract_body_text(payload: dict) -> str:
    """Extract plain text body from Gmail message payload.

    Walks the MIME tree looking for text/plain parts. Falls back to
    stripping HTML tags from text/html if no plain text part exists.
    """
    mime_type = payload.get("mimeType", "")

    # Simple message with body data directly
    if mime_type == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    # Multipart — recurse into parts
    parts = payload.get("parts", [])

    # First pass: look for text/plain
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    # Second pass: recurse into nested multipart
    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            result = _extract_body_text(part)
            if result:
                return result

    # Fallback: strip HTML tags from text/html
    for part in parts:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", "", html).strip()

    # Direct body on non-text/plain (e.g., text/html at top level)
    if payload.get("body", {}).get("data"):
        data = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        if "text/html" in mime_type:
            return re.sub(r"<[^>]+>", "", data).strip()
        return data

    return ""


def parse_message(raw: dict) -> Message:
    """Parse a raw Gmail API message resource into our Message model."""
    headers = raw.get("payload", {}).get("headers", [])
    date_str = _get_header(headers, "Date")

    try:
        parsed_date = email.utils.parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        parsed_date = datetime.now(tz=UTC)

    return Message(
        id=raw["id"],
        thread_id=raw["threadId"],
        subject=_decode_header(_get_header(headers, "Subject")),
        **{"from": parse_email_address(_get_header(headers, "From"))},
        to=parse_email_address_list(_get_header(headers, "To")),
        cc=parse_email_address_list(_get_header(headers, "Cc")),
        date=parsed_date,
        body_text=_extract_body_text(raw.get("payload", {})),
        snippet=raw.get("snippet", ""),
        label_ids=raw.get("labelIds", []),
        message_id_header=_get_header(headers, "Message-ID") or None,
    )
