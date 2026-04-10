"""Async Gmail API client using per-user OAuth credentials."""

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from googleapiclient.errors import HttpError

from api.gmail import _transport
from api.gmail.exceptions import (
    GmailApiError,
    GmailAuthError,
    GmailNotFoundError,
    GmailRateLimitError,
    GmailValidationError,
)
from api.gmail.models import Draft, HistoryRecord, Message, Thread, parse_message

if TYPE_CHECKING:
    from api.gmail.auth import TokenStore

logger = logging.getLogger(__name__)


def _map_http_error(exc: HttpError) -> GmailApiError:
    """Map a Google API HttpError to a domain-specific exception."""
    status = exc.resp.status
    message = str(exc)
    if status == 404:
        return GmailNotFoundError(message, status_code=status)
    if status in (401, 403):
        return GmailAuthError(message, status_code=status)
    if status == 429:
        return GmailRateLimitError(message, status_code=status)
    return GmailApiError(message, status_code=status)


def _build_raw_message(
    from_email: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Build a base64url-encoded RFC 2822 message."""
    msg = MIMEText(body)
    msg["From"] = from_email
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class GmailClient:
    """Async Gmail API client using per-user OAuth credentials."""

    def __init__(self, token_store: TokenStore):
        self._token_store = token_store

    async def has_credentials(self, user_email: str) -> bool:
        """Check whether stored credentials exist for a user."""
        if not self._token_store:
            return False
        return await self._token_store.has_token(user_email)

    async def _get_creds(self, user_email: str):
        return await self._token_store.load_credentials(user_email)

    async def _exec(self, user_email: str, fn):
        """Load credentials and execute a Gmail API call."""
        creds = await self._get_creds(user_email)
        try:
            return await _transport.execute(creds, fn)
        except HttpError as exc:
            raise _map_http_error(exc) from exc

    # --- Read ---

    async def get_message(self, user_email: str, message_id: str) -> Message:
        """Fetch and parse a single message."""
        logger.info("get_message user=%s message_id=%s", user_email, message_id)
        raw = await self._exec(
            user_email,
            lambda svc: (
                svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            ),
        )
        return parse_message(raw)

    async def get_thread(self, user_email: str, thread_id: str) -> Thread:
        """Fetch all messages in a thread, ordered chronologically."""
        logger.info("get_thread user=%s thread_id=%s", user_email, thread_id)
        raw = await self._exec(
            user_email,
            lambda svc: (
                svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
            ),
        )
        messages = [parse_message(m) for m in raw.get("messages", [])]
        messages.sort(key=lambda m: m.date)
        return Thread(id=raw["id"], messages=messages)

    # --- Draft ---

    async def create_draft(
        self,
        user_email: str,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> Draft:
        """Create a draft. If thread_id is set, creates it as a reply in that thread."""
        if not to or not all(addr.strip() for addr in to):
            raise GmailValidationError(
                "create_draft requires at least one non-empty recipient address"
            )
        logger.info("create_draft user=%s thread_id=%s", user_email, thread_id)
        raw_msg = _build_raw_message(
            from_email=user_email,
            to=to,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=in_reply_to,
        )
        draft_body: dict = {"message": {"raw": raw_msg}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        raw = await self._exec(
            user_email,
            lambda svc: svc.users().drafts().create(userId="me", body=draft_body).execute(),
        )
        # Fetch the full draft to get parsed message content
        return await self._get_draft(user_email, raw["id"])

    async def update_draft(
        self,
        user_email: str,
        draft_id: str,
        to: list[str],
        subject: str,
        body: str,
    ) -> Draft:
        """Replace the content of an existing draft."""
        logger.info("update_draft user=%s draft_id=%s", user_email, draft_id)
        raw_msg = _build_raw_message(from_email=user_email, to=to, subject=subject, body=body)
        draft_body = {"message": {"raw": raw_msg}}

        raw = await self._exec(
            user_email,
            lambda svc: (
                svc.users().drafts().update(userId="me", id=draft_id, body=draft_body).execute()
            ),
        )
        return await self._get_draft(user_email, raw["id"])

    async def delete_draft(self, user_email: str, draft_id: str) -> None:
        """Delete a draft."""
        logger.info("delete_draft user=%s draft_id=%s", user_email, draft_id)
        await self._exec(
            user_email,
            lambda svc: svc.users().drafts().delete(userId="me", id=draft_id).execute(),
        )

    async def send_draft(self, user_email: str, draft_id: str) -> Message:
        """Send an existing draft. Returns the sent message."""
        logger.info("send_draft user=%s draft_id=%s", user_email, draft_id)
        raw = await self._exec(
            user_email,
            lambda svc: svc.users().drafts().send(userId="me", body={"id": draft_id}).execute(),
        )
        # drafts.send returns a Message resource
        return await self.get_message(user_email, raw["id"])

    async def _get_draft(self, user_email: str, draft_id: str) -> Draft:
        """Fetch a draft with its full message content."""
        raw = await self._exec(
            user_email,
            lambda svc: svc.users().drafts().get(userId="me", id=draft_id, format="full").execute(),
        )
        return Draft(id=raw["id"], message=parse_message(raw["message"]))

    # --- Send ---

    async def send_message(
        self,
        user_email: str,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> Message:
        """Compose and send a message directly (no draft step)."""
        if not to or not all(addr.strip() for addr in to):
            raise GmailValidationError(
                "send_message requires at least one non-empty recipient address"
            )
        logger.info("send_message user=%s to=%s", user_email, to)
        raw_msg = _build_raw_message(
            from_email=user_email,
            to=to,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=in_reply_to,
        )
        msg_body: dict = {"raw": raw_msg}
        if thread_id:
            msg_body["threadId"] = thread_id

        raw = await self._exec(
            user_email,
            lambda svc: svc.users().messages().send(userId="me", body=msg_body).execute(),
        )
        return await self.get_message(user_email, raw["id"])

    # --- Push Notifications ---

    async def watch(self, user_email: str, topic_name: str) -> dict:
        """Register Pub/Sub push notifications for a coordinator's mailbox.

        Returns dict with historyId, expiration.
        """
        logger.info("watch user=%s topic=%s", user_email, topic_name)
        body = {
            "topicName": topic_name,
            "labelIds": ["INBOX"],
        }
        return await self._exec(
            user_email,
            lambda svc: svc.users().watch(userId="me", body=body).execute(),
        )

    async def stop_watch(self, user_email: str) -> None:
        """Stop push notifications for a coordinator's mailbox."""
        logger.info("stop_watch user=%s", user_email)
        await self._exec(
            user_email,
            lambda svc: svc.users().stop(userId="me").execute(),
        )

    async def history_list(
        self,
        user_email: str,
        start_history_id: str,
        history_types: list[str] | None = None,
    ) -> dict:
        """List history records since a given historyId.

        Returns dict with history records and historyId.
        history_types defaults to ['messageAdded'].
        """
        if history_types is None:
            history_types = ["messageAdded"]
        logger.info("history_list user=%s start=%s", user_email, start_history_id)

        raw = await self._exec(
            user_email,
            lambda svc: (
                svc.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=history_types,
                )
                .execute()
            ),
        )

        records: list[HistoryRecord] = []
        for entry in raw.get("history", []):
            added = [m["message"]["id"] for m in entry.get("messagesAdded", [])]
            deleted = [m["message"]["id"] for m in entry.get("messagesDeleted", [])]
            if added or deleted:
                records.append(HistoryRecord(messages_added=added, messages_deleted=deleted))

        return {
            "history": records,
            "historyId": raw.get("historyId", start_history_id),
        }

    async def get_message_metadata(self, user_email: str, message_id: str) -> dict:
        """Get message metadata (headers only, no body) for quick pre-filtering.

        Uses format='metadata' to minimize API quota.
        """
        logger.info("get_message_metadata user=%s message_id=%s", user_email, message_id)
        raw = await self._exec(
            user_email,
            lambda svc: (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date", "Message-ID"],
                )
                .execute()
            ),
        )
        headers = raw.get("payload", {}).get("headers", [])
        header_dict = {h["name"]: h["value"] for h in headers}
        return {
            "id": raw["id"],
            "threadId": raw["threadId"],
            "labelIds": raw.get("labelIds", []),
            "headers": header_dict,
        }
