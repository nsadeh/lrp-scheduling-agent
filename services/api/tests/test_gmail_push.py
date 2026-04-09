"""Unit tests for Gmail push notification methods (watch, history, metadata)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.gmail.client import GmailClient
from api.gmail.models import HistoryRecord

_PATCH = "api.gmail._transport.execute"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_token_store():
    store = MagicMock()
    store.load_credentials = AsyncMock(return_value=MagicMock())
    return store


@pytest.fixture()
def client(mock_token_store):
    return GmailClient(mock_token_store)


# ---------------------------------------------------------------------------
# GmailClient.watch
# ---------------------------------------------------------------------------


class TestWatch:
    @pytest.mark.asyncio()
    async def test_watch_calls_api(self, client: GmailClient):
        expected = {
            "historyId": "12345",
            "expiration": "1680000000000",
        }

        with patch(_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = expected
            result = await client.watch(
                "coord@lrp.com",
                "projects/my-project/topics/gmail",
            )

        assert result == expected
        mock_exec.assert_awaited_once()
        # Verify the lambda calls the right API method
        fn = mock_exec.call_args[0][1]
        svc = MagicMock()
        fn(svc)
        svc.users().watch.assert_called_once_with(
            userId="me",
            body={
                "topicName": "projects/my-project/topics/gmail",
                "labelIds": ["INBOX"],
            },
        )

    @pytest.mark.asyncio()
    async def test_watch_returns_history_and_expiration(self, client: GmailClient):
        expected = {
            "historyId": "99999",
            "expiration": "1700000000000",
        }
        with patch(_PATCH, new_callable=AsyncMock, return_value=expected):
            result = await client.watch("coord@lrp.com", "projects/p/topics/t")

        assert result["historyId"] == "99999"
        assert result["expiration"] == "1700000000000"


# ---------------------------------------------------------------------------
# GmailClient.stop_watch
# ---------------------------------------------------------------------------


class TestStopWatch:
    @pytest.mark.asyncio()
    async def test_stop_watch_calls_api(self, client: GmailClient):
        with patch(_PATCH, new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = None
            await client.stop_watch("coord@lrp.com")

        mock_exec.assert_awaited_once()
        fn = mock_exec.call_args[0][1]
        svc = MagicMock()
        fn(svc)
        svc.users().stop.assert_called_once_with(userId="me")


# ---------------------------------------------------------------------------
# GmailClient.history_list
# ---------------------------------------------------------------------------


class TestHistoryList:
    @pytest.mark.asyncio()
    async def test_history_list_parses_records(self, client: GmailClient):
        raw_response = {
            "history": [
                {
                    "id": "100",
                    "messagesAdded": [
                        {"message": {"id": "msg_a", "threadId": "thr_1"}},
                        {"message": {"id": "msg_b", "threadId": "thr_2"}},
                    ],
                },
                {
                    "id": "101",
                    "messagesDeleted": [
                        {"message": {"id": "msg_c", "threadId": "thr_3"}},
                    ],
                },
            ],
            "historyId": "102",
        }

        with patch(_PATCH, new_callable=AsyncMock, return_value=raw_response):
            result = await client.history_list("coord@lrp.com", "99")

        assert result["historyId"] == "102"
        records = result["history"]
        assert len(records) == 2
        assert isinstance(records[0], HistoryRecord)
        assert records[0].messages_added == ["msg_a", "msg_b"]
        assert records[0].messages_deleted == []
        assert records[1].messages_added == []
        assert records[1].messages_deleted == ["msg_c"]

    @pytest.mark.asyncio()
    async def test_history_list_empty_history(self, client: GmailClient):
        raw_response = {"historyId": "50"}

        with patch(_PATCH, new_callable=AsyncMock, return_value=raw_response):
            result = await client.history_list("coord@lrp.com", "50")

        assert result["history"] == []
        assert result["historyId"] == "50"

    @pytest.mark.asyncio()
    async def test_history_list_default_types(self, client: GmailClient):
        ret = {"historyId": "1"}
        with patch(_PATCH, new_callable=AsyncMock, return_value=ret) as mock_exec:
            await client.history_list("coord@lrp.com", "1")

        fn = mock_exec.call_args[0][1]
        svc = MagicMock()
        fn(svc)
        svc.users().history().list.assert_called_once_with(
            userId="me",
            startHistoryId="1",
            historyTypes=["messageAdded"],
        )

    @pytest.mark.asyncio()
    async def test_history_list_custom_types(self, client: GmailClient):
        ret = {"historyId": "1"}
        with patch(_PATCH, new_callable=AsyncMock, return_value=ret) as mock_exec:
            await client.history_list(
                "coord@lrp.com",
                "1",
                history_types=["messageAdded", "messageDeleted"],
            )

        fn = mock_exec.call_args[0][1]
        svc = MagicMock()
        fn(svc)
        svc.users().history().list.assert_called_once_with(
            userId="me",
            startHistoryId="1",
            historyTypes=["messageAdded", "messageDeleted"],
        )


# ---------------------------------------------------------------------------
# GmailClient.get_message_metadata
# ---------------------------------------------------------------------------


class TestGetMessageMetadata:
    @pytest.mark.asyncio()
    async def test_get_message_metadata_returns_headers(self, client: GmailClient):
        raw_response = {
            "id": "msg_123",
            "threadId": "thr_456",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "coord@lrp.com"},
                    {"name": "Subject", "value": "Interview Request"},
                    {
                        "name": "Date",
                        "value": "Mon, 30 Mar 2026 12:00:00 +0000",
                    },
                    {"name": "Message-ID", "value": "<abc@example.com>"},
                ],
            },
        }

        with patch(_PATCH, new_callable=AsyncMock, return_value=raw_response):
            result = await client.get_message_metadata("coord@lrp.com", "msg_123")

        assert result["id"] == "msg_123"
        assert result["threadId"] == "thr_456"
        assert result["labelIds"] == ["INBOX", "UNREAD"]
        assert result["headers"]["From"] == "sender@example.com"
        assert result["headers"]["Subject"] == "Interview Request"

    @pytest.mark.asyncio()
    async def test_get_message_metadata_uses_metadata_format(self, client: GmailClient):
        ret = {"id": "m1", "threadId": "t1", "payload": {"headers": []}}
        with patch(_PATCH, new_callable=AsyncMock, return_value=ret) as mock_exec:
            await client.get_message_metadata("coord@lrp.com", "m1")

        fn = mock_exec.call_args[0][1]
        svc = MagicMock()
        fn(svc)
        svc.users().messages().get.assert_called_once_with(
            userId="me",
            id="m1",
            format="metadata",
            metadataHeaders=[
                "From",
                "To",
                "Subject",
                "Date",
                "Message-ID",
            ],
        )


# ---------------------------------------------------------------------------
# TokenStore watch-state methods
# ---------------------------------------------------------------------------


class TestTokenStoreWatchState:
    """Test TokenStore push notification state methods using a mock pool."""

    @pytest.fixture()
    def mock_pool(self):
        """Create a mock async connection pool."""
        pool = MagicMock()
        conn = AsyncMock()
        cursor = AsyncMock()
        conn.execute = AsyncMock(return_value=cursor)
        # Make pool.connection() work as async context manager
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.connection = MagicMock(return_value=ctx)
        return pool, conn, cursor

    @pytest.fixture()
    def token_store(self, mock_pool):
        from cryptography.fernet import Fernet

        from api.gmail.auth import TokenStore

        pool, _, _ = mock_pool
        return TokenStore(db_pool=pool, encryption_key=Fernet.generate_key())

    @pytest.mark.asyncio()
    async def test_update_watch_state(self, token_store, mock_pool):
        _, conn, _ = mock_pool
        expiry = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        await token_store.update_watch_state("coord@lrp.com", "12345", expiry)

        conn.execute.assert_awaited_once()
        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "last_history_id" in sql
        assert "watch_expiry" in sql
        assert params["email"] == "coord@lrp.com"
        assert params["history_id"] == "12345"
        assert params["watch_expiry"] == expiry

    @pytest.mark.asyncio()
    async def test_get_history_id(self, token_store, mock_pool):
        _, _, cursor = mock_pool
        cursor.fetchone = AsyncMock(return_value=("54321",))
        result = await token_store.get_history_id("coord@lrp.com")
        assert result == "54321"

    @pytest.mark.asyncio()
    async def test_get_history_id_none(self, token_store, mock_pool):
        _, _, cursor = mock_pool
        cursor.fetchone = AsyncMock(return_value=None)
        result = await token_store.get_history_id("nobody@lrp.com")
        assert result is None

    @pytest.mark.asyncio()
    async def test_update_history_id(self, token_store, mock_pool):
        _, conn, _ = mock_pool
        await token_store.update_history_id("coord@lrp.com", "99999")

        conn.execute.assert_awaited_once()
        params = conn.execute.call_args[0][1]
        assert params["history_id"] == "99999"
        assert params["email"] == "coord@lrp.com"

    @pytest.mark.asyncio()
    async def test_get_watch_state(self, token_store, mock_pool):
        _, _, cursor = mock_pool
        expiry = datetime(2026, 4, 10, tzinfo=UTC)
        cursor.fetchone = AsyncMock(return_value=("12345", expiry))
        hid, wexp = await token_store.get_watch_state("coord@lrp.com")
        assert hid == "12345"
        assert wexp == expiry

    @pytest.mark.asyncio()
    async def test_get_watch_state_no_row(self, token_store, mock_pool):
        _, _, cursor = mock_pool
        cursor.fetchone = AsyncMock(return_value=None)
        hid, wexp = await token_store.get_watch_state("nobody@lrp.com")
        assert hid is None
        assert wexp is None

    @pytest.mark.asyncio()
    async def test_get_all_coordinators_with_tokens(self, token_store, mock_pool):
        _, _, cursor = mock_pool
        cursor.fetchall = AsyncMock(return_value=[("a@lrp.com",), ("b@lrp.com",)])
        result = await token_store.get_all_coordinators_with_tokens()
        assert result == ["a@lrp.com", "b@lrp.com"]

    @pytest.mark.asyncio()
    async def test_get_all_coordinators_empty(self, token_store, mock_pool):
        _, _, cursor = mock_pool
        cursor.fetchall = AsyncMock(return_value=[])
        result = await token_store.get_all_coordinators_with_tokens()
        assert result == []
