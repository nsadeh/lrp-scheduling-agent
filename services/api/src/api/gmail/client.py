"""Async Gmail API client using per-user OAuth credentials."""

from __future__ import annotations

import base64
import logging
import sys
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
from api.gmail.models import Draft, Message, Thread, parse_message

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

    async def has_token(self, user_email: str) -> bool:
        """Check if a user has stored credentials."""
        return await self._token_store.has_token(user_email)

    async def _get_creds(self, user_email: str):
        return await self._token_store.load_credentials(user_email)

    async def _exec(self, user_email: str, fn):
        """Load credentials and execute a Gmail API call.

        The calling public method's name (e.g. ``watch``, ``send_message``) is
        forwarded to ``_transport.execute`` as the Sentry span ``op_name`` so
        traces distinguish individual Gmail operations without threading the
        name through every call site.
        """
        caller_name = sys._getframe(1).f_code.co_name
        creds = await self._get_creds(user_email)
        try:
            return await _transport.execute(creds, fn, op_name=caller_name)
        except HttpError as exc:
            raise _map_http_error(exc) from exc

    # --- Push pipeline ---

    async def watch(self, user_email: str, topic_name: str) -> dict:
        """Register Pub/Sub push notifications for a mailbox.

        Returns {"historyId": "...", "expiration": "..."}.
        Watches all labels — scheduling replies may be auto-archived or labeled.
        """
        logger.info("watch user=%s topic=%s", user_email, topic_name)
        return await self._exec(
            user_email,
            lambda svc: (
                svc.users()
                .watch(
                    userId="me",
                    body={"topicName": topic_name, "labelIds": None},
                )
                .execute()
            ),
        )

    async def stop_watch(self, user_email: str) -> None:
        """Unregister push notifications for a mailbox."""
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
        """Fetch mailbox changes since a history ID.

        Returns {"history": [...], "historyId": "latest_id"}.
        On 404 (expired historyId), raises GmailNotFoundError.
        """
        logger.info("history_list user=%s start=%s", user_email, start_history_id)
        kwargs: dict = {
            "userId": "me",
            "startHistoryId": start_history_id,
        }
        if history_types:
            kwargs["historyTypes"] = history_types
        return await self._exec(
            user_email,
            lambda svc: svc.users().history().list(**kwargs).execute(),
        )

    async def get_profile(self, user_email: str) -> dict:
        """Fetch user profile — used to get initial historyId.

        Returns {"emailAddress": "...", "historyId": "...", ...}.
        """
        logger.info("get_profile user=%s", user_email)
        return await self._exec(
            user_email,
            lambda svc: svc.users().getProfile(userId="me").execute(),
        )

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
