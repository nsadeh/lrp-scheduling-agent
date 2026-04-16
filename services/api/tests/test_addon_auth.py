"""Tests for Google Workspace Add-on user token verification."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from api.addon.auth import verify_google_addon_token


def _mock_request(body: dict | None = None) -> MagicMock:
    """Create a mock FastAPI Request with the given body."""

    req = MagicMock()
    req.url = "https://example.com/addon/homepage"

    async def mock_json():
        return body or {}

    req.json = mock_json
    return req


def _body_with_token(email: str = "nim@longridgepartners.com") -> dict:
    """Build a request body with a valid-format userIdToken."""
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode()
    return {
        "authorizationEventObject": {
            "userIdToken": f"header.{payload}.signature",
        },
    }


class TestVerifyGoogleAddonToken:
    async def test_missing_user_id_token_returns_401(self):
        req = _mock_request(body={"commonEventObject": {}})
        with pytest.raises(HTTPException) as exc_info:
            await verify_google_addon_token(req)
        assert exc_info.value.status_code == 401

    async def test_empty_body_returns_401(self):
        req = _mock_request(body={})
        with pytest.raises(HTTPException) as exc_info:
            await verify_google_addon_token(req)
        assert exc_info.value.status_code == 401

    async def test_valid_token_returns_claims(self):
        body = _body_with_token("nim@longridgepartners.com")
        fake_claims = {"iss": "accounts.google.com", "email": "nim@longridgepartners.com"}
        req = _mock_request(body=body)

        with patch("api.addon.auth.id_token.verify_token", return_value=fake_claims):
            result = await verify_google_addon_token(req)

        assert result["email"] == "nim@longridgepartners.com"

    async def test_invalid_token_returns_401(self):
        body = _body_with_token()
        req = _mock_request(body=body)

        with (
            patch("api.addon.auth.id_token.verify_token", side_effect=ValueError("bad")),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_google_addon_token(req)
        assert exc_info.value.status_code == 401

    async def test_token_without_email_returns_401(self):
        body = _body_with_token()
        req = _mock_request(body=body)

        # Token verifies but has no email claim
        with (
            patch(
                "api.addon.auth.id_token.verify_token", return_value={"iss": "accounts.google.com"}
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_google_addon_token(req)
        assert exc_info.value.status_code == 401
