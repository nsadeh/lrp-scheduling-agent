#!/usr/bin/env python3
"""Integration test script: exercises all GmailClient methods against real Gmail.

Usage:
    uv run python scripts/test_gmail_integration.py \
        --user nim@kinematiclabs.dev --recipient nim.sadeh@hey.com

Requires:
    - OAuth token stored for --user (run gmail_oauth.py first)
    - GMAIL_TOKEN_ENCRYPTION_KEY, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET in env
    - DATABASE_URL pointing to Postgres
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from api.gmail.auth import TokenStore  # noqa: E402
from api.gmail.client import GmailClient  # noqa: E402


def _ok(label: str):
    print(f"  ✓ {label}")


def _fail(label: str, err: Exception):
    print(f"  ✗ {label}: {err}")


async def run_tests(user_email: str, recipient: str):
    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    encryption_key = os.environ["GMAIL_TOKEN_ENCRYPTION_KEY"]

    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()

    try:
        token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
        gmail = GmailClient(token_store)

        timestamp = int(time.time())
        subject = f"LRP Gmail Library Test — {timestamp}"

        # --- Test 1: Send a direct message ---
        print("\n1. Send a direct message")
        sent_msg = await gmail.send_message(
            user_email=user_email,
            to=[recipient],
            subject=subject,
            body=f"This is an automated test from the LRP Gmail Library.\n\nTimestamp: {timestamp}",
        )
        _ok(f"Sent message id={sent_msg.id}, thread_id={sent_msg.thread_id}")

        # --- Test 2: Read the sent message ---
        print("\n2. Read the sent message back")
        read_msg = await gmail.get_message(user_email, sent_msg.id)
        assert read_msg.subject == subject, f"Subject mismatch: {read_msg.subject}"
        assert recipient in [a.email for a in read_msg.to], "Recipient not in To"
        _ok(f"Subject: {read_msg.subject}")
        _ok(f"From: {read_msg.from_.email}")
        _ok(f"Body preview: {read_msg.body_text[:80]!r}")

        # --- Test 3: Read the thread ---
        print("\n3. Read the thread")
        thread = await gmail.get_thread(user_email, sent_msg.thread_id)
        _ok(f"Thread has {len(thread.messages)} message(s)")

        # --- Test 4: Create a draft reply ---
        print("\n4. Create a draft reply in the thread")
        draft = await gmail.create_draft(
            user_email=user_email,
            to=[recipient],
            subject=f"Re: {subject}",
            body="This is a draft reply — testing create_draft.",
            thread_id=sent_msg.thread_id,
            in_reply_to=read_msg.message_id_header,
        )
        _ok(f"Draft created id={draft.id}")

        # --- Test 5: Update the draft ---
        print("\n5. Update the draft")
        updated_draft = await gmail.update_draft(
            user_email=user_email,
            draft_id=draft.id,
            to=[recipient],
            subject=f"Re: {subject}",
            body="This is the UPDATED draft reply — testing update_draft.",
        )
        _ok(f"Draft updated id={updated_draft.id}")

        # --- Test 6: Send the draft ---
        print("\n6. Send the draft")
        sent_reply = await gmail.send_draft(user_email, draft.id)
        _ok(f"Draft sent as message id={sent_reply.id}")

        # --- Test 7: Read thread again — should have 2 messages ---
        print("\n7. Read thread again (should have 2 messages)")
        thread2 = await gmail.get_thread(user_email, sent_msg.thread_id)
        _ok(f"Thread now has {len(thread2.messages)} message(s)")
        for i, m in enumerate(thread2.messages):
            _ok(f"  [{i}] {m.subject} — {m.date.isoformat()}")

        print(f"\n{'='*60}")
        print("ALL TESTS PASSED")
        print(f"Thread ID: {sent_msg.thread_id}")
        print(f"Search query: subject:({subject})")
        print(f"{'='*60}")

    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description="Gmail integration tests")
    parser.add_argument("--user", required=True, help="Gmail user to act as")
    parser.add_argument("--recipient", required=True, help="Email address to send test mail to")
    args = parser.parse_args()

    asyncio.run(run_tests(args.user, args.recipient))


if __name__ == "__main__":
    main()
