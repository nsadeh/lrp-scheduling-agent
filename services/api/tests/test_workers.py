"""Tests for arq background worker functions."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.agent.workers import (
    cleanup_old_processed_messages,
    process_gmail_notification,
    process_relevant_message,
    renew_gmail_watches,
    sync_gmail_history,
)
from api.gmail.models import EmailAddress, HistoryRecord, Message

_NOW = datetime.datetime.now(tz=datetime.UTC)


def _make_message(
    msg_id: str = "msg_1",
    thread_id: str = "thread_1",
    subject: str = "Schedule interview with Jane",
    sender_email: str = "recruiter@lrp.com",
) -> Message:
    return Message(
        id=msg_id,
        thread_id=thread_id,
        subject=subject,
        from_=EmailAddress(email=sender_email),
        to=[],
        date=_NOW,
        body_text="Please schedule an interview.",
        snippet="Please schedule an interview.",
        label_ids=["INBOX"],
        message_id_header=None,
    )


def _mock_db_pool():
    """Create a mock AsyncConnectionPool that supports async context manager."""
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.connection = MagicMock(return_value=ctx)
    return pool


def _make_ctx(
    *,
    stored_history_id: str | None = "100",
    history_records: list[HistoryRecord] | None = None,
    new_history_id: str = "200",
    is_processed: bool = False,
    is_relevant: bool = True,
    relevance_reason: str = "keyword:interview",
    coordinators: list[str] | None = None,
) -> dict:
    """Build a mock arq worker context dict."""
    if history_records is None:
        history_records = [HistoryRecord(messages_added=["msg_new_1"], messages_deleted=[])]
    if coordinators is None:
        coordinators = ["coord@lrp.com"]

    token_store = AsyncMock()
    token_store.get_history_id = AsyncMock(return_value=stored_history_id)
    token_store.update_history_id = AsyncMock()
    token_store.get_all_coordinators_with_tokens = AsyncMock(return_value=coordinators)
    token_store.update_watch_state = AsyncMock()

    gmail = AsyncMock()
    gmail.history_list = AsyncMock(
        return_value={"history": history_records, "historyId": new_history_id}
    )
    gmail.get_message_metadata = AsyncMock(
        return_value={
            "id": "msg_new_1",
            "threadId": "thread_1",
            "labelIds": ["INBOX"],
            "headers": {"From": "someone@example.com", "Subject": "Interview"},
        }
    )
    gmail.get_message = AsyncMock(return_value=_make_message())
    gmail.get_thread = AsyncMock(return_value=MagicMock(id="thread_1", messages=[_make_message()]))
    _expiration = str(int(_NOW.timestamp() * 1000) + 86400000)
    gmail.watch = AsyncMock(return_value={"historyId": "300", "expiration": _expiration})

    db = _mock_db_pool()

    redis = AsyncMock()
    redis.enqueue_job = AsyncMock(return_value=MagicMock(job_id="job_123"))
    redis.set = AsyncMock(return_value=True)  # debounce lock acquired

    scheduling = AsyncMock()
    scheduling.find_loop_by_thread = AsyncMock(return_value=None)

    return {
        "token_store": token_store,
        "gmail": gmail,
        "db": db,
        "redis": redis,
        "scheduling": scheduling,
    }


# ============================================================
# process_gmail_notification
# ============================================================


class TestProcessGmailNotification:
    async def test_fetches_history_and_processes_new_messages(self):
        ctx = _make_ctx()

        with patch("api.agent.workers.queries") as mock_queries:
            mock_queries.is_message_processed = AsyncMock(return_value=False)
            mock_queries.mark_message_processed = AsyncMock()

            with patch("api.agent.workers.is_scheduling_relevant") as mock_filter:
                mock_filter.return_value = (True, "keyword:interview")
                await process_gmail_notification(ctx, "coord@lrp.com", "150")

        # Should use stored history_id (100), not push history_id (150)
        ctx["gmail"].history_list.assert_called_once_with(
            "coord@lrp.com", "100", history_types=["messageAdded"]
        )
        # Should enqueue processing for relevant message
        ctx["redis"].enqueue_job.assert_called_once_with(
            "process_relevant_message", "coord@lrp.com", "msg_new_1", "thread_1"
        )
        # Should update history_id
        ctx["token_store"].update_history_id.assert_called_once_with("coord@lrp.com", "200")

    async def test_falls_back_to_push_history_id(self):
        ctx = _make_ctx(stored_history_id=None)

        with patch("api.agent.workers.queries") as mock_queries:
            mock_queries.is_message_processed = AsyncMock(return_value=False)
            mock_queries.mark_message_processed = AsyncMock()

            with patch("api.agent.workers.is_scheduling_relevant") as mock_filter:
                mock_filter.return_value = (True, "keyword:interview")
                await process_gmail_notification(ctx, "coord@lrp.com", "150")

        # Should fall back to push history_id
        ctx["gmail"].history_list.assert_called_once_with(
            "coord@lrp.com", "150", history_types=["messageAdded"]
        )

    async def test_skips_already_processed_messages(self):
        ctx = _make_ctx()

        with patch("api.agent.workers.queries") as mock_queries:
            mock_queries.is_message_processed = AsyncMock(return_value=True)
            mock_queries.mark_message_processed = AsyncMock()

            with patch("api.agent.workers.is_scheduling_relevant") as mock_filter:
                await process_gmail_notification(ctx, "coord@lrp.com", "100")

        # Should NOT call pre-filter or enqueue
        mock_filter.assert_not_called()
        ctx["redis"].enqueue_job.assert_not_called()
        # Should still update history_id
        ctx["token_store"].update_history_id.assert_called_once()

    async def test_skips_irrelevant_messages(self):
        ctx = _make_ctx()

        with patch("api.agent.workers.queries") as mock_queries:
            mock_queries.is_message_processed = AsyncMock(return_value=False)
            mock_queries.mark_message_processed = AsyncMock()

            with patch("api.agent.workers.is_scheduling_relevant") as mock_filter:
                mock_filter.return_value = (False, "no_match")
                await process_gmail_notification(ctx, "coord@lrp.com", "100")

        ctx["redis"].enqueue_job.assert_not_called()

    async def test_handles_empty_history(self):
        ctx = _make_ctx(history_records=[], new_history_id="100")

        with patch("api.agent.workers.queries"):
            await process_gmail_notification(ctx, "coord@lrp.com", "100")

        ctx["redis"].enqueue_job.assert_not_called()
        ctx["token_store"].update_history_id.assert_called_once_with("coord@lrp.com", "100")

    async def test_multiple_messages_in_history(self):
        records = [
            HistoryRecord(messages_added=["msg_a", "msg_b"], messages_deleted=[]),
            HistoryRecord(messages_added=["msg_c"], messages_deleted=[]),
        ]
        ctx = _make_ctx(history_records=records)

        call_count = 0

        with patch("api.agent.workers.queries") as mock_queries:
            mock_queries.is_message_processed = AsyncMock(return_value=False)
            mock_queries.mark_message_processed = AsyncMock()

            with patch("api.agent.workers.is_scheduling_relevant") as mock_filter:
                mock_filter.return_value = (True, "keyword:schedule")
                await process_gmail_notification(ctx, "coord@lrp.com", "100")
                call_count = ctx["redis"].enqueue_job.call_count

        assert call_count == 3


# ============================================================
# process_relevant_message
# ============================================================


class TestProcessRelevantMessage:
    async def test_acquires_debounce_lock_and_processes(self):
        ctx = _make_ctx()

        await process_relevant_message(ctx, "coord@lrp.com", "msg_1", "thread_1")

        # Should acquire debounce lock
        ctx["redis"].set.assert_called_once_with("debounce:thread_1", "1", ex=60, nx=True)
        # Should fetch full message and thread
        ctx["gmail"].get_message.assert_called_once_with("coord@lrp.com", "msg_1")
        ctx["gmail"].get_thread.assert_called_once_with("coord@lrp.com", "thread_1")
        # Should look for matching loop
        ctx["scheduling"].find_loop_by_thread.assert_called_once_with("thread_1")

    async def test_skips_when_debounce_lock_held(self):
        ctx = _make_ctx()
        ctx["redis"].set = AsyncMock(return_value=False)  # lock NOT acquired

        await process_relevant_message(ctx, "coord@lrp.com", "msg_1", "thread_1")

        # Should NOT fetch message or thread
        ctx["gmail"].get_message.assert_not_called()
        ctx["gmail"].get_thread.assert_not_called()

    async def test_raises_on_gmail_error(self):
        ctx = _make_ctx()
        ctx["gmail"].get_message = AsyncMock(side_effect=RuntimeError("API error"))

        with pytest.raises(RuntimeError, match="API error"):
            await process_relevant_message(ctx, "coord@lrp.com", "msg_1", "thread_1")


# ============================================================
# renew_gmail_watches
# ============================================================


class TestRenewGmailWatches:
    async def test_renews_watches_for_all_coordinators(self):
        ctx = _make_ctx(coordinators=["a@lrp.com", "b@lrp.com"])

        await renew_gmail_watches(ctx)

        assert ctx["gmail"].watch.call_count == 2
        assert ctx["token_store"].update_watch_state.call_count == 2

    async def test_continues_on_failure(self):
        ctx = _make_ctx(coordinators=["a@lrp.com", "b@lrp.com"])
        # First call fails, second succeeds
        ctx["gmail"].watch = AsyncMock(
            side_effect=[
                RuntimeError("Auth error"),
                {"historyId": "300", "expiration": str(int(_NOW.timestamp() * 1000) + 86400000)},
            ]
        )

        await renew_gmail_watches(ctx)

        # Should still attempt second coordinator
        assert ctx["gmail"].watch.call_count == 2
        # Only one successful update
        assert ctx["token_store"].update_watch_state.call_count == 1

    async def test_no_coordinators(self):
        ctx = _make_ctx(coordinators=[])

        await renew_gmail_watches(ctx)

        ctx["gmail"].watch.assert_not_called()


# ============================================================
# sync_gmail_history
# ============================================================


class TestSyncGmailHistory:
    async def test_syncs_history_for_all_coordinators(self):
        ctx = _make_ctx(coordinators=["a@lrp.com", "b@lrp.com"])

        with patch("api.agent.workers.queries") as mock_queries:
            mock_queries.is_message_processed = AsyncMock(return_value=False)
            mock_queries.mark_message_processed = AsyncMock()

            with patch("api.agent.workers.is_scheduling_relevant") as mock_filter:
                mock_filter.return_value = (True, "keyword:schedule")
                await sync_gmail_history(ctx)

        assert ctx["gmail"].history_list.call_count == 2

    async def test_skips_coordinators_without_history_id(self):
        ctx = _make_ctx(coordinators=["a@lrp.com"])
        ctx["token_store"].get_history_id = AsyncMock(return_value=None)

        await sync_gmail_history(ctx)

        ctx["gmail"].history_list.assert_not_called()

    async def test_continues_on_failure(self):
        ctx = _make_ctx(coordinators=["a@lrp.com", "b@lrp.com"])
        ctx["gmail"].history_list = AsyncMock(
            side_effect=[RuntimeError("Boom"), {"history": [], "historyId": "200"}]
        )

        await sync_gmail_history(ctx)

        # Should attempt both coordinators
        assert ctx["gmail"].history_list.call_count == 2


# ============================================================
# cleanup_old_processed_messages
# ============================================================


class TestCleanupOldProcessedMessages:
    async def test_calls_cleanup_query(self):
        ctx = _make_ctx()

        with patch("api.agent.workers.queries") as mock_queries:
            mock_queries.cleanup_old_processed_messages = AsyncMock()
            await cleanup_old_processed_messages(ctx)

            mock_queries.cleanup_old_processed_messages.assert_called_once()
