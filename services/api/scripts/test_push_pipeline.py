#!/usr/bin/env python3
"""E2E test for the Gmail push pipeline.

Exercises the full flow:
1. Registers a Pub/Sub watch for the coordinator
2. Sends a test email to the coordinator's inbox
3. Waits briefly, then runs a history sync (simulating poll fallback)
4. Verifies the EmailEvent fires through the hook

Usage:
    uv run python scripts/test_push_pipeline.py \
        --user nim@kinematiclabs.dev \
        --sender nim@kinematiclabs.dev

Requires:
    - OAuth token stored for --user (run gmail_oauth.py first)
    - GMAIL_TOKEN_ENCRYPTION_KEY, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
    - DATABASE_URL, REDIS_URL, PUBSUB_TOPIC in env
    - Pub/Sub topic created with gmail-api-push SA as publisher
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from api.gmail.auth import TokenStore  # noqa: E402
from api.gmail.client import GmailClient  # noqa: E402
from api.gmail.hooks import (  # noqa: E402
    EmailEvent,
    MessageDirection,
    MessageType,
    classify_direction,
    classify_message_type,
)


class CapturingHook:
    """Test hook that captures events for verification."""

    def __init__(self):
        self.events: list[EmailEvent] = []

    async def on_email(self, event: EmailEvent) -> None:
        self.events.append(event)
        print(
            f"  ✓ EVENT: direction={event.direction.value} "
            f"type={event.message_type.value} "
            f"thread={event.message.thread_id} "
            f"subject={event.message.subject!r} "
            f"from={event.message.from_.email}"
        )
        if event.new_participants:
            names = [p.email for p in event.new_participants]
            print(f"    new_participants={names}")


async def run_test(user_email: str, sender_email: str):
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    pubsub_topic = os.environ.get("PUBSUB_TOPIC", "")

    if not encryption_key:
        print("✗ GMAIL_TOKEN_ENCRYPTION_KEY not set")
        return

    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()
    token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
    gmail = GmailClient(token_store)
    hook = CapturingHook()

    try:
        # Step 0: Verify we have credentials
        print(f"\n{'='*60}")
        print("Push Pipeline E2E Test")
        print(f"{'='*60}")
        print(f"  Coordinator: {user_email}")
        print(f"  Sender: {sender_email}")
        print()

        has_token = await token_store.has_token(user_email)
        if not has_token:
            print(f"✗ No OAuth token for {user_email}. Run gmail_oauth.py first.")
            return
        print(f"✓ OAuth token found for {user_email}")

        # Step 1: Register Pub/Sub watch
        if pubsub_topic:
            print("\n--- Step 1: Register Pub/Sub watch ---")
            try:
                result = await gmail.watch(user_email, pubsub_topic)
                expiry = datetime.fromtimestamp(int(result["expiration"]) / 1000, tz=UTC)
                history_id = result["historyId"]
                await token_store.update_watch_state(user_email, history_id, expiry)
                print(f"✓ Watch registered, history_id={history_id}, expires={expiry}")
            except Exception as e:
                print(f"✗ Watch registration failed: {e}")
                print("  Continuing with poll-only mode...")
        else:
            print("\n--- Step 1: Skipping watch (PUBSUB_TOPIC not set) ---")

        # Step 2: Get current history baseline
        print("\n--- Step 2: Establish history baseline ---")
        profile = await gmail.get_profile(user_email)
        baseline_history_id = profile["historyId"]
        await token_store.update_history_id(user_email, str(baseline_history_id))
        print(f"✓ Baseline history_id={baseline_history_id}")

        # Step 3: Send a test email
        print("\n--- Step 3: Send test email ---")
        timestamp = int(time.time())
        subject = f"[Push Pipeline Test] {timestamp}"
        body = (
            f"This is an automated test of the Gmail push pipeline.\n"
            f"Timestamp: {timestamp}\n"
            f"If you see this, the send path works."
        )
        sent_msg = await gmail.send_message(
            user_email,
            to=[sender_email],
            subject=subject,
            body=body,
        )
        print(f"✓ Sent message id={sent_msg.id} thread={sent_msg.thread_id}")
        print(f"  Subject: {subject}")

        # Step 4: Wait for Gmail to process
        print("\n--- Step 4: Wait for Gmail to index (10s) ---")
        await asyncio.sleep(10)
        print("✓ Done waiting")

        # Step 5: Run history sync (simulating what the worker does)
        print("\n--- Step 5: Process history (simulating poll) ---")
        from api.gmail.exceptions import GmailNotFoundError

        try:
            history_response = await gmail.history_list(
                user_email,
                str(baseline_history_id),
                history_types=["messageAdded"],
            )
        except GmailNotFoundError:
            print("✗ History ID expired — this shouldn't happen for a fresh baseline")
            return

        new_message_ids = []
        for entry in history_response.get("history", []):
            for msg_added in entry.get("messagesAdded", []):
                msg_id = msg_added.get("message", {}).get("id")
                if msg_id:
                    new_message_ids.append(msg_id)

        print(f"✓ Found {len(new_message_ids)} new message(s) since baseline")

        if not new_message_ids:
            print("✗ No new messages found. Gmail may not have indexed yet.")
            print("  Try increasing the wait time or check Gmail directly.")
            return

        # Step 6: Process each message through classification + hook
        print("\n--- Step 6: Classify and fire hook ---")
        threads_cache = {}

        for msg_id in new_message_ids:
            message = await gmail.get_message(user_email, msg_id)

            thread_id = message.thread_id
            if thread_id not in threads_cache:
                thread = await gmail.get_thread(user_email, thread_id)
                threads_cache[thread_id] = thread.messages
            thread_messages = threads_cache[thread_id]

            direction = classify_direction(message, user_email)
            prior = [m for m in thread_messages if m.id != message.id and m.date < message.date]
            msg_type, new_participants = classify_message_type(message, prior)

            event = EmailEvent(
                message=message,
                coordinator_email=user_email,
                direction=direction,
                message_type=msg_type,
                new_participants=new_participants,
            )
            await hook.on_email(event)

        # Step 7: Verify results
        print("\n--- Step 7: Results ---")
        print(f"✓ Processed {len(hook.events)} event(s)")

        # Check that our sent message was classified correctly
        our_event = None
        for evt in hook.events:
            if evt.message.subject == subject:
                our_event = evt
                break

        if our_event:
            print("✓ Found our test message in events")
            assert (
                our_event.direction == MessageDirection.OUTGOING
            ), f"Expected OUTGOING, got {our_event.direction}"
            print("✓ Direction correctly classified as OUTGOING")
            assert (
                our_event.message_type == MessageType.NEW_THREAD
            ), f"Expected NEW_THREAD, got {our_event.message_type}"
            print("✓ Message type correctly classified as NEW_THREAD")
        else:
            print("⚠ Our test message not found in events (may be in a different history batch)")
            print("  Events found:")
            for evt in hook.events:
                print(f"    - {evt.message.subject!r} ({evt.direction.value})")

        # Update history cursor
        new_history = history_response.get("historyId")
        if new_history:
            await token_store.update_history_id(user_email, str(new_history))

        print(f"\n{'='*60}")
        print("✓ Push pipeline E2E test complete!")
        print(f"{'='*60}")

    finally:
        await pool.close()


async def run_incoming_test(user_email: str):
    """Test incoming email classification.

    Skips sending (assumes an external email was already sent to the coordinator).
    Just runs history sync and classifies whatever new messages arrived.
    """
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")

    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()
    token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
    gmail = GmailClient(token_store)
    hook = CapturingHook()

    try:
        print(f"\n{'='*60}")
        print("Incoming Email Detection Test")
        print(f"{'='*60}")

        stored_history_id = await token_store.get_history_id(user_email)
        if not stored_history_id:
            print("✗ No history baseline — run the full test first")
            return
        print(f"✓ Using history_id={stored_history_id}")

        print("\n--- Processing history ---")
        from api.gmail.exceptions import GmailNotFoundError

        try:
            history_response = await gmail.history_list(
                user_email,
                str(stored_history_id),
                history_types=["messageAdded"],
            )
        except GmailNotFoundError:
            print("✗ History ID expired")
            return

        new_message_ids = []
        for entry in history_response.get("history", []):
            for msg_added in entry.get("messagesAdded", []):
                msg_id = msg_added.get("message", {}).get("id")
                if msg_id:
                    new_message_ids.append(msg_id)

        print(f"✓ Found {len(new_message_ids)} new message(s)")

        if not new_message_ids:
            print("⚠ No new messages. Send an email to the coordinator first.")
            return

        threads_cache = {}
        for msg_id in new_message_ids:
            message = await gmail.get_message(user_email, msg_id)
            thread_id = message.thread_id
            if thread_id not in threads_cache:
                thread = await gmail.get_thread(user_email, thread_id)
                threads_cache[thread_id] = thread.messages
            thread_messages = threads_cache[thread_id]

            direction = classify_direction(message, user_email)
            prior = [m for m in thread_messages if m.id != message.id and m.date < message.date]
            msg_type, new_participants = classify_message_type(message, prior)

            event = EmailEvent(
                message=message,
                coordinator_email=user_email,
                direction=direction,
                message_type=msg_type,
                new_participants=new_participants,
            )
            await hook.on_email(event)

        print("\n--- Results ---")
        for evt in hook.events:
            status = "✓" if evt.direction == MessageDirection.INCOMING else "·"
            print(
                f"  {status} {evt.direction.value} {evt.message_type.value}"
                f" — {evt.message.subject!r}"
            )

        incoming = [e for e in hook.events if e.direction == MessageDirection.INCOMING]
        print(f"\n✓ {len(incoming)} incoming, {len(hook.events) - len(incoming)} outgoing")

        new_history = history_response.get("historyId")
        if new_history:
            await token_store.update_history_id(user_email, str(new_history))

        print(f"{'='*60}")
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description="Test the Gmail push pipeline e2e")
    parser.add_argument("--user", required=True, help="Coordinator email (must have OAuth token)")
    parser.add_argument(
        "--sender",
        help="Email to send test message to (defaults to --user for self-send)",
    )
    parser.add_argument(
        "--incoming-only",
        action="store_true",
        help="Skip sending, just process new incoming messages",
    )
    args = parser.parse_args()
    if args.incoming_only:
        asyncio.run(run_incoming_test(args.user))
    else:
        sender = args.sender or args.user
        asyncio.run(run_test(args.user, sender))


if __name__ == "__main__":
    main()
