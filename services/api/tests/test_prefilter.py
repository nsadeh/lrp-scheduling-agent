"""Tests for the scheduling relevance pre-filter."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.agent.prefilter import is_scheduling_relevant
from api.gmail.models import EmailAddress, Message

_NOW = datetime.datetime.now(tz=datetime.UTC)


def _make_message(
    subject: str = "Hello",
    snippet: str = "",
    sender_email: str = "someone@example.com",
    thread_id: str = "thread_1",
) -> Message:
    return Message(
        id="msg_1",
        thread_id=thread_id,
        subject=subject,
        from_=EmailAddress(email=sender_email),
        to=[],
        date=_NOW,
        body_text="",
        snippet=snippet,
        label_ids=[],
        message_id_header=None,
    )


@pytest.fixture
def mock_db_pool():
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.connection = MagicMock(return_value=ctx)
    return pool


@pytest.fixture
def no_loop():
    """find_loop_by_thread that always returns None."""
    return AsyncMock(return_value=None)


@pytest.fixture
def has_loop():
    """find_loop_by_thread that always returns a loop."""
    return AsyncMock(return_value={"id": "lop_123"})


async def test_known_thread_is_relevant(mock_db_pool, has_loop):
    msg = _make_message(subject="Unrelated subject")
    relevant, reason = await is_scheduling_relevant(msg, mock_db_pool, has_loop)
    assert relevant is True
    assert reason == "known_thread"


async def test_known_sender_is_relevant(mock_db_pool, no_loop):
    msg = _make_message(subject="Lunch plans")
    with patch("api.agent.prefilter.queries") as mock_queries:
        mock_queries.has_known_contact = AsyncMock(return_value=True)
        relevant, reason = await is_scheduling_relevant(msg, mock_db_pool, no_loop)
    assert relevant is True
    assert reason == "known_sender"


async def test_keyword_match_is_relevant(mock_db_pool, no_loop):
    msg = _make_message(subject="Schedule interview with John Smith")
    with patch("api.agent.prefilter.queries") as mock_queries:
        mock_queries.has_known_contact = AsyncMock(return_value=False)
        relevant, reason = await is_scheduling_relevant(msg, mock_db_pool, no_loop)
    assert relevant is True
    assert reason.startswith("keyword:")


async def test_keyword_in_snippet_is_relevant(mock_db_pool, no_loop):
    msg = _make_message(
        subject="Re: follow up",
        snippet="Please send your availability for next week",
    )
    with patch("api.agent.prefilter.queries") as mock_queries:
        mock_queries.has_known_contact = AsyncMock(return_value=False)
        relevant, reason = await is_scheduling_relevant(msg, mock_db_pool, no_loop)
    assert relevant is True
    assert reason == "keyword:availability"


async def test_unrelated_email_not_relevant(mock_db_pool, no_loop):
    msg = _make_message(
        subject="Updated JD for role",
        snippet="Please review the attached document",
    )
    with patch("api.agent.prefilter.queries") as mock_queries:
        mock_queries.has_known_contact = AsyncMock(return_value=False)
        relevant, reason = await is_scheduling_relevant(msg, mock_db_pool, no_loop)
    assert relevant is False
    assert reason == "no_match"


async def test_case_insensitive_keyword_match(mock_db_pool, no_loop):
    msg = _make_message(subject="RE: INTERVIEW SCHEDULE")
    with patch("api.agent.prefilter.queries") as mock_queries:
        mock_queries.has_known_contact = AsyncMock(return_value=False)
        relevant, _reason = await is_scheduling_relevant(msg, mock_db_pool, no_loop)
    assert relevant is True
