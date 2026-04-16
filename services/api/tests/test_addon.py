"""Integration tests for Google Workspace Add-on endpoints."""

import base64
import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.addon.auth import verify_google_addon_token
from api.main import app
from api.scheduling.models import StatusBoard


async def _mock_addon_auth():
    """Mock auth dependency — returns stub claims."""
    return {"iss": "accounts.google.com", "email": "service-account@gserviceaccount.com"}


# Override the auth dependency for all tests
app.dependency_overrides[verify_google_addon_token] = _mock_addon_auth

# Build a fake JWT with an email claim for test requests
_TEST_EMAIL = "test@longridgepartners.com"
_jwt_payload = base64.urlsafe_b64encode(json.dumps({"email": _TEST_EMAIL}).encode()).decode()
_FAKE_USER_ID_TOKEN = f"header.{_jwt_payload}.signature"


@pytest.fixture
def mock_scheduling():
    """Mock the LoopService to avoid needing a real database."""
    svc = AsyncMock()
    svc.get_status_board = AsyncMock(return_value=StatusBoard())
    svc.find_loop_by_thread = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def mock_overview():
    """Mock the OverviewService to avoid needing a real database."""
    from api.overview.service import OverviewService

    svc = AsyncMock(spec=OverviewService)
    svc.get_overview_data = AsyncMock(return_value=[])
    svc.get_thread_overview_data = AsyncMock(return_value=[])
    return svc


@pytest.fixture
async def client(mock_scheduling, mock_overview):
    app.state.scheduling = mock_scheduling
    app.state.overview_service = mock_overview
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


MINIMAL_EVENT = {
    "commonEventObject": {
        "hostApp": "GMAIL",
        "platform": "WEB",
    },
    "authorizationEventObject": {
        "userIdToken": _FAKE_USER_ID_TOKEN,
    },
}

MESSAGE_EVENT = {
    "commonEventObject": {
        "hostApp": "GMAIL",
        "platform": "WEB",
    },
    "authorizationEventObject": {
        "userIdToken": _FAKE_USER_ID_TOKEN,
    },
    "gmail": {
        "messageId": "msg-abc-123",
        "threadId": "thread-xyz-789",
    },
}


def _get_card(data: dict) -> dict:
    """Extract the card from the response — homepage/on-message use pushCard."""
    nav = data["action"]["navigations"][0]
    return nav.get("pushCard") or nav.get("updateCard")


class TestHomepage:
    async def test_returns_valid_card(self, client: AsyncClient):
        resp = await client.post("/addon/homepage", json=MINIMAL_EVENT)
        assert resp.status_code == 200
        card = _get_card(resp.json())
        assert len(card["sections"]) > 0

    async def test_empty_overview_shows_caught_up(self, client: AsyncClient):
        resp = await client.post("/addon/homepage", json=MINIMAL_EVENT)
        card = _get_card(resp.json())
        text = str(card)
        assert "caught up" in text.lower()


class TestOnMessage:
    async def test_unlinked_thread_shows_create_prompt(self, client: AsyncClient, mock_overview):
        mock_overview.get_thread_overview_data = AsyncMock(return_value=[])
        resp = await client.post("/addon/on-message", json=MESSAGE_EVENT)
        assert resp.status_code == 200
        card = _get_card(resp.json())
        widgets_text = str(card)
        assert "not linked" in widgets_text.lower() or "create" in widgets_text.lower()

    async def test_falls_back_to_overview_without_gmail(self, client: AsyncClient):
        """When no gmail context is present, falls back to full overview."""
        resp = await client.post("/addon/on-message", json=MINIMAL_EVENT)
        assert resp.status_code == 200
        card = _get_card(resp.json())
        text = str(card)
        assert "caught up" in text.lower()


class TestAction:
    async def test_show_create_form(self, client: AsyncClient):
        event = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
                "invokedFunction": "show_create_form",
            },
            "authorizationEventObject": {
                "userIdToken": _FAKE_USER_ID_TOKEN,
            },
        }
        resp = await client.post("/addon/action", json=event)
        assert resp.status_code == 200
        data = resp.json()
        card = data["action"]["navigations"][0]["updateCard"]
        assert card["header"]["title"] == "New Scheduling Loop"

    async def test_unknown_function_returns_status_board(self, client: AsyncClient):
        event = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
                "invokedFunction": "nonexistent_function",
            },
            "authorizationEventObject": {
                "userIdToken": _FAKE_USER_ID_TOKEN,
            },
        }
        resp = await client.post("/addon/action", json=event)
        assert resp.status_code == 200


class TestRefresh:
    async def test_returns_self_closing_html(self, client: AsyncClient):
        resp = await client.get("/addon/refresh")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "window.close()" in resp.text


class TestStaticFiles:
    async def test_logo_served(self, client: AsyncClient):
        resp = await client.get("/static/logo.png")
        assert resp.status_code == 200
        assert "image" in resp.headers["content-type"]
