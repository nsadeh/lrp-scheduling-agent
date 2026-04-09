"""Tests for the Gmail Pub/Sub webhook endpoint."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.agent.webhook import PubSubMessage


def _encode_pubsub_data(email_address: str, history_id: str) -> str:
    """Create base64-encoded Pub/Sub data payload."""
    data = {"emailAddress": email_address, "historyId": history_id}
    return base64.b64encode(json.dumps(data).encode()).decode()


class TestPubSubMessage:
    def test_parse_valid_message(self):
        body = {
            "message": {
                "data": _encode_pubsub_data("coord@lrp.com", "12345"),
                "messageId": "msg_001",
            },
            "subscription": "projects/my-project/subscriptions/gmail-push",
        }
        msg = PubSubMessage.from_request_body(body)
        assert msg is not None
        assert msg.email_address == "coord@lrp.com"
        assert msg.history_id == "12345"

    def test_parse_missing_data(self):
        body = {"message": {}, "subscription": "projects/my-project/subscriptions/gmail-push"}
        msg = PubSubMessage.from_request_body(body)
        assert msg is None

    def test_parse_invalid_base64(self):
        body = {
            "message": {"data": "not-valid-base64!!!"},
            "subscription": "projects/my-project/subscriptions/gmail-push",
        }
        msg = PubSubMessage.from_request_body(body)
        assert msg is None

    def test_parse_missing_email(self):
        data = {"historyId": "123"}
        body = {
            "message": {
                "data": base64.b64encode(json.dumps(data).encode()).decode(),
            },
        }
        msg = PubSubMessage.from_request_body(body)
        assert msg is None

    def test_parse_empty_body(self):
        msg = PubSubMessage.from_request_body({})
        assert msg is None


@pytest.fixture
def app():
    """Create a minimal FastAPI app with the webhook router."""
    from fastapi import FastAPI

    from api.agent.webhook import webhook_router

    test_app = FastAPI()
    test_app.include_router(webhook_router)

    # Mock token_store
    mock_token_store = AsyncMock()
    mock_token_store.has_token = AsyncMock(return_value=True)
    test_app.state.token_store = mock_token_store

    # Mock redis (arq pool)
    mock_redis = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "test_job_123"
    mock_redis.enqueue_job = AsyncMock(return_value=mock_job)
    test_app.state.redis = mock_redis

    return test_app


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


async def test_webhook_valid_notification(client, app):
    body = {
        "message": {
            "data": _encode_pubsub_data("coord@lrp.com", "12345"),
            "messageId": "msg_001",
        },
        "subscription": "projects/my-project/subscriptions/gmail-push",
    }
    response = await client.post("/webhook/gmail", json=body)
    assert response.status_code == 200

    # Verify job was enqueued
    app.state.redis.enqueue_job.assert_called_once_with(
        "process_gmail_notification",
        "coord@lrp.com",
        "12345",
    )


async def test_webhook_unparseable_message(client, app):
    response = await client.post("/webhook/gmail", json={"garbage": True})
    assert response.status_code == 200  # Always 200 to prevent retries
    app.state.redis.enqueue_job.assert_not_called()


async def test_webhook_unknown_coordinator(client, app):
    app.state.token_store.has_token = AsyncMock(return_value=False)
    body = {
        "message": {
            "data": _encode_pubsub_data("unknown@example.com", "999"),
            "messageId": "msg_002",
        },
    }
    response = await client.post("/webhook/gmail", json=body)
    assert response.status_code == 200
    app.state.redis.enqueue_job.assert_not_called()


async def test_webhook_no_redis(client, app):
    app.state.redis = None
    body = {
        "message": {
            "data": _encode_pubsub_data("coord@lrp.com", "12345"),
            "messageId": "msg_001",
        },
    }
    response = await client.post("/webhook/gmail", json=body)
    assert response.status_code == 200  # Graceful degradation
