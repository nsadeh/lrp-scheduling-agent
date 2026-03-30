"""Integration tests for Google Workspace Add-on endpoints."""

import os

import pytest
from httpx import ASGITransport, AsyncClient

# Set SKIP_ADDON_AUTH before importing the app so the module-level check picks it up
os.environ["SKIP_ADDON_AUTH"] = "true"

from api.main import app


@pytest.fixture
async def client():
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
        card = data["action"]["navigations"][0]["pushCard"]
        assert card["header"]["title"] == "LRP Scheduling Agent"
        assert card["header"]["subtitle"] == "Long Ridge Partners"

    async def test_sections_contain_welcome_text(self, client: AsyncClient):
        resp = await client.post("/addon/homepage", json=MINIMAL_EVENT)
        data = resp.json()
        card = data["action"]["navigations"][0]["pushCard"]
        text = card["sections"][0]["widgets"][0]["textParagraph"]["text"]
        assert "scheduling" in text.lower()


class TestOnMessage:
    async def test_returns_card_with_message_id(self, client: AsyncClient):
        resp = await client.post("/addon/on-message", json=MESSAGE_EVENT)
        assert resp.status_code == 200
        data = resp.json()
        card = data["action"]["navigations"][0]["pushCard"]
        section = card["sections"][0]
        assert section["header"] == "Message Context"
        # The message ID should appear somewhere in the card text
        widget_text = section["widgets"][0]["textParagraph"]["text"]
        assert "msg-abc-123" in widget_text

    async def test_falls_back_to_homepage_without_gmail(self, client: AsyncClient):
        """When no gmail context is present, falls back to homepage card."""
        resp = await client.post("/addon/on-message", json=MINIMAL_EVENT)
        assert resp.status_code == 200
        data = resp.json()
        card = data["action"]["navigations"][0]["pushCard"]
        # Homepage card has no "Message Context" section header
        assert card["sections"][0].get("header") is None

    async def test_falls_back_without_message_id(self, client: AsyncClient):
        """Gmail context present but no messageId falls back to homepage."""
        event = {
            "commonEventObject": {"hostApp": "GMAIL", "platform": "WEB"},
            "gmail": {"threadId": "thread-only"},
        }
        resp = await client.post("/addon/on-message", json=event)
        assert resp.status_code == 200
        data = resp.json()
        card = data["action"]["navigations"][0]["pushCard"]
        assert card["sections"][0].get("header") is None


class TestStaticFiles:
    async def test_logo_served(self, client: AsyncClient):
        resp = await client.get("/static/logo.png")
        assert resp.status_code == 200
        assert "image" in resp.headers["content-type"]
