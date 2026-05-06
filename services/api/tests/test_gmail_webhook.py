"""Unit tests for the Gmail Pub/Sub webhook endpoint."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def _mock_verify():
    """Patch OIDC verification to always succeed."""
    with patch("api.gmail.webhook._verify_pubsub_token", new_callable=AsyncMock) as mock:
        mock.return_value = {"email": "gmail-api-push@system.gserviceaccount.com"}
        yield mock


@pytest.fixture
def _mock_verify_fail():
    """Patch OIDC verification to always fail."""
    with patch("api.gmail.webhook._verify_pubsub_token", new_callable=AsyncMock) as mock:
        mock.side_effect = ValueError("Invalid token")
        yield mock


def _make_pubsub_body(email: str = "coordinator@lrp.com", history_id: str = "12345") -> dict:
    """Build a Pub/Sub push notification body."""
    data = base64.b64encode(
        json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    ).decode()
    return {"message": {"data": data}}


@pytest.fixture
def app():
    """Create a lightweight test app with only the webhook router — no lifespan."""
    from fastapi import FastAPI

    from api.gmail.webhook import webhook_router

    test_app = FastAPI()
    test_app.include_router(webhook_router)

    # Mock Gmail client — has_token must be AsyncMock since it's awaited
    mock_gmail = MagicMock()
    mock_gmail.has_token = AsyncMock(return_value=True)
    mock_gmail._token_store.is_token_stale = AsyncMock(return_value=False)
    test_app.state.gmail = mock_gmail

    # Mock Redis
    mock_redis = AsyncMock()
    mock_redis.enqueue_job = AsyncMock()
    test_app.state.redis = mock_redis

    return test_app


class TestWebhook:
    def test_valid_push_enqueues_job(self, app, _mock_verify):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/webhook/gmail", json=_make_pubsub_body())
        assert response.status_code == 200
        app.state.redis.enqueue_job.assert_called_once_with(
            "process_gmail_push",
            "coordinator@lrp.com",
            "12345",
        )

    def test_auth_failure_returns_200(self, app, _mock_verify_fail):
        """Auth failures still return 200 to prevent Pub/Sub retries."""
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/webhook/gmail", json=_make_pubsub_body())
        assert response.status_code == 200
        app.state.redis.enqueue_job.assert_not_called()

    def test_unknown_coordinator_skips(self, app, _mock_verify):
        """Push for unknown coordinator is silently ignored."""
        app.state.gmail.has_token = AsyncMock(return_value=False)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/webhook/gmail", json=_make_pubsub_body())
        assert response.status_code == 200
        app.state.redis.enqueue_job.assert_not_called()

    def test_malformed_body_returns_200(self, app, _mock_verify):
        """Malformed data still returns 200 to prevent Pub/Sub retries."""
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/webhook/gmail", json={"message": {"data": "not-base64!!!"}})
        assert response.status_code == 200

    def test_missing_redis_returns_200(self, app, _mock_verify):
        """If Redis is down, we log and return 200."""
        app.state.redis = None
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/webhook/gmail", json=_make_pubsub_body())
        assert response.status_code == 200

    def test_missing_email_or_history_id_returns_200(self, app, _mock_verify):
        """Missing fields in notification are handled gracefully."""
        data = base64.b64encode(json.dumps({}).encode()).decode()
        body = {"message": {"data": data}}
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/webhook/gmail", json=body)
        assert response.status_code == 200
        app.state.redis.enqueue_job.assert_not_called()
