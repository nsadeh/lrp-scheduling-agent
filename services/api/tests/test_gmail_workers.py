"""Tests for the Gmail push worker — focused on draft filtering in _process_history.

Regression coverage for the "truncated email" bug: Gmail fires messageAdded
history events for draft auto-saves, and the pipeline used to process them,
running the agent on half-typed bodies. Drafts must be skipped.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.gmail.models import Message, Thread
from api.gmail.workers import _process_history, run_next_action_agent


def _make_message(msg_id: str, *, labels: list[str], thread_id: str = "thread1") -> Message:
    return Message(
        id=msg_id,
        thread_id=thread_id,
        subject="Re: Feedback",
        **{"from": {"name": "Adam", "email": "adam@longridgepartners.com"}},
        to=[{"name": "Paul", "email": "paul@burkehillglobal.com"}],
        date=datetime(2026, 5, 21, 9, 39, tzinfo=UTC),
        body_text="hello",
        label_ids=labels,
    )


class _ConnCtx:
    """Async context manager that yields a throwaway connection mock."""

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *_args):
        return False


async def _empty_processed(*_args, **_kwargs):
    """Async generator yielding no already-processed IDs (all are new)."""
    return
    yield  # pragma: no cover — makes this an async generator


def _build_ctx():
    """Assemble a mocked worker ctx and return (ctx, mocks) for assertions."""
    gmail = MagicMock()
    gmail.history_list = AsyncMock()
    gmail.get_message = AsyncMock()
    gmail.get_thread = AsyncMock(return_value=Thread(id="thread1", messages=[]))

    token_store = MagicMock()
    token_store.update_history_id = AsyncMock()

    pool = MagicMock()
    pool.connection = lambda: _ConnCtx()

    router = MagicMock()
    router.on_email = AsyncMock()

    ctx = {
        "gmail": gmail,
        "token_store": token_store,
        "db": pool,
        "router": router,
        "redis": None,
    }
    return ctx, gmail, token_store, router


@pytest.fixture
def _mock_queries():
    """Patch the aiosql query object: nothing pre-processed, mark is a no-op."""
    with patch("api.gmail.workers.gmail_queries") as mq:
        mq.get_processed_message_ids = lambda *a, **k: _empty_processed()
        mq.mark_messages_processed_batch = AsyncMock()
        yield mq


@pytest.mark.asyncio
async def test_draft_skipped_normal_processed(_mock_queries):
    """A DRAFT messageAdded is dropped at extraction; the SENT one is processed."""
    ctx, gmail, _token_store, router = _build_ctx()
    gmail.history_list.return_value = {
        "history": [
            {"messagesAdded": [{"message": {"id": "draft1", "labelIds": ["DRAFT"]}}]},
            {"messagesAdded": [{"message": {"id": "sent1", "labelIds": ["SENT"]}}]},
        ],
        "historyId": "999",
    }
    gmail.get_message.return_value = _make_message("sent1", labels=["SENT"])

    await _process_history(ctx, "adam@longridgepartners.com", "100")

    # Only the real message is fetched and routed.
    gmail.get_message.assert_awaited_once_with("adam@longridgepartners.com", "sent1")
    router.on_email.assert_awaited_once()

    # The draft ID is never marked processed (so a reused ID can't be deduped).
    marked = _mock_queries.mark_messages_processed_batch.await_args.kwargs["message_ids"]
    assert marked == ["sent1"]
    assert "draft1" not in marked


@pytest.mark.asyncio
async def test_draft_only_history_advances_cursor(_mock_queries):
    """If every added message is a draft, nothing runs but the cursor advances."""
    ctx, gmail, token_store, router = _build_ctx()
    gmail.history_list.return_value = {
        "history": [
            {"messagesAdded": [{"message": {"id": "draft1", "labelIds": ["DRAFT"]}}]},
        ],
        "historyId": "999",
    }

    await _process_history(ctx, "adam@longridgepartners.com", "100")

    gmail.get_message.assert_not_awaited()
    router.on_email.assert_not_awaited()
    _mock_queries.mark_messages_processed_batch.assert_not_awaited()
    token_store.update_history_id.assert_awaited_once_with("adam@longridgepartners.com", "999")


@pytest.mark.asyncio
async def test_draft_backstop_after_fetch(_mock_queries):
    """Stub lacks labelIds, but the fetched message is a draft → skipped post-fetch."""
    ctx, gmail, _token_store, router = _build_ctx()
    gmail.history_list.return_value = {
        "history": [
            {"messagesAdded": [{"message": {"id": "mystery1"}}]},  # no labelIds in stub
        ],
        "historyId": "999",
    }
    gmail.get_message.return_value = _make_message("mystery1", labels=["DRAFT"])

    await _process_history(ctx, "adam@longridgepartners.com", "100")

    gmail.get_message.assert_awaited_once()
    router.on_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_sent_message_processes(_mock_queries):
    """Sanity: a plain outbound SENT message is not over-filtered."""
    ctx, gmail, _token_store, router = _build_ctx()
    gmail.history_list.return_value = {
        "history": [
            {"messagesAdded": [{"message": {"id": "sent1", "labelIds": ["SENT"]}}]},
        ],
        "historyId": "999",
    }
    gmail.get_message.return_value = _make_message("sent1", labels=["SENT"])

    await _process_history(ctx, "adam@longridgepartners.com", "100")

    router.on_email.assert_awaited_once()


@pytest.mark.asyncio
async def test_next_action_explicit_draft_id_falls_back():
    """An explicit gmail_message_id pointing at a draft must not be the trigger.

    Falls back to the latest non-draft message in the thread.
    """
    ctx, gmail, _token_store, router = _build_ctx()
    gmail.get_message.return_value = _make_message("draft1", labels=["DRAFT"])
    gmail.get_thread.return_value = Thread(
        id="thread1", messages=[_make_message("sent1", labels=["SENT"])]
    )

    await run_next_action_agent(ctx, "adam@longridgepartners.com", "draft1", "thread1")

    router.on_email.assert_awaited_once()
    triggered = router.on_email.await_args.args[0]
    assert triggered.message.id == "sent1"


@pytest.mark.asyncio
async def test_next_action_explicit_nondraft_id_used():
    """A non-draft explicit gmail_message_id is used directly as the trigger."""
    ctx, gmail, _token_store, router = _build_ctx()
    gmail.get_message.return_value = _make_message("sent1", labels=["SENT"])
    gmail.get_thread.return_value = Thread(
        id="thread1", messages=[_make_message("sent1", labels=["SENT"])]
    )

    await run_next_action_agent(ctx, "adam@longridgepartners.com", "sent1", "thread1")

    router.on_email.assert_awaited_once()
    triggered = router.on_email.await_args.args[0]
    assert triggered.message.id == "sent1"
