"""Integration tests for Google Workspace Add-on endpoints."""

import os
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

# Set SKIP_ADDON_AUTH before importing the app so the module-level check picks it up
os.environ["SKIP_ADDON_AUTH"] = "true"

from api.main import app
from api.scheduling.models import StatusBoard


@pytest.fixture
def mock_scheduling():
    """Mock the LoopService to avoid needing a real database."""
    svc = AsyncMock()
    svc.get_status_board = AsyncMock(return_value=StatusBoard())
    svc.find_loop_by_thread = AsyncMock(return_value=None)
    return svc


@pytest.fixture
async def client(mock_scheduling):
    app.state.scheduling = mock_scheduling
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


MINIMAL_EVENT = {
    "commonEventObject": {
        "hostApp": "GMAIL",
        "platform": "WEB",
    }
}

MESSAGE_EVENT = {
    "commonEventObject": {
        "hostApp": "GMAIL",
        "platform": "WEB",
    },
    "gmail": {
        "messageId": "msg-abc-123",
        "threadId": "thread-xyz-789",
    },
}


class TestHomepage:
    async def test_returns_valid_card(self, client: AsyncClient):
        resp = await client.post("/addon/homepage", json=MINIMAL_EVENT)
        assert resp.status_code == 200
        data = resp.json()
        card = data["action"]["navigations"][0]["updateCard"]
        # Header removed — sidebar already shows "LRP Scheduling Agent"
        assert "header" not in card

    async def test_sections_present(self, client: AsyncClient):
        resp = await client.post("/addon/homepage", json=MINIMAL_EVENT)
        data = resp.json()
        card = data["action"]["navigations"][0]["updateCard"]
        assert len(card["sections"]) > 0


class TestOnMessage:
    async def test_unlinked_thread_shows_create_prompt(self, client: AsyncClient):
        resp = await client.post("/addon/on-message", json=MESSAGE_EVENT)
        assert resp.status_code == 200
        data = resp.json()
        card = data["action"]["navigations"][0]["updateCard"]
        # Should show "not linked" message since mock returns None
        widgets_text = str(card)
        assert "not linked" in widgets_text.lower() or "create" in widgets_text.lower()

    async def test_falls_back_to_status_board_without_gmail(self, client: AsyncClient):
        """When no gmail context is present, falls back to status board."""
        resp = await client.post("/addon/on-message", json=MINIMAL_EVENT)
        assert resp.status_code == 200
        data = resp.json()
        card = data["action"]["navigations"][0]["updateCard"]
        assert "header" not in card


class TestAction:
    async def test_show_create_form(self, client: AsyncClient):
        event = {
            "commonEventObject": {
                "hostApp": "GMAIL",
                "platform": "WEB",
                "invokedFunction": "show_create_form",
            }
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
            }
        }
        resp = await client.post("/addon/action", json=event)
        assert resp.status_code == 200


class TestStaticFiles:
    async def test_logo_served(self, client: AsyncClient):
        resp = await client.get("/static/logo.png")
        assert resp.status_code == 200
        assert "image" in resp.headers["content-type"]
