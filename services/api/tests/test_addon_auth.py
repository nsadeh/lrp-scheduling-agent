"""Tests for Google Workspace Add-on token verification."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from api.addon.auth import verify_google_addon_token


def _mock_request(url: str = "https://example.com/addon/homepage") -> MagicMock:
    """Create a mock FastAPI Request with the given URL."""
    req = MagicMock()
    req.url = url
    return req


class TestVerifyGoogleAddonToken:
    async def test_missing_bearer_prefix_returns_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await verify_google_addon_token(
                request=_mock_request(), authorization="not-a-bearer-token"
            )
        assert exc_info.value.status_code == 401

    async def test_empty_authorization_returns_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await verify_google_addon_token(request=_mock_request(), authorization="")
        assert exc_info.value.status_code == 401

    async def test_invalid_token_returns_401(self):
        with (
            patch("api.addon.auth.id_token.verify_token", side_effect=ValueError("bad token")),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_google_addon_token(
                request=_mock_request(), authorization="Bearer fake.token.here"
            )
        assert exc_info.value.status_code == 401

    async def test_wrong_issuer_returns_401(self):
        fake_claims = {"iss": "https://evil.example.com", "email": "good@gcp-sa.com"}
        with (
            patch("api.addon.auth.id_token.verify_token", return_value=fake_claims),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_google_addon_token(
                request=_mock_request(), authorization="Bearer valid.token.here"
            )
        assert exc_info.value.status_code == 401

    async def test_wrong_email_returns_401(self, monkeypatch):
        monkeypatch.setattr("api.addon.auth.GOOGLE_ADDON_SA_EMAIL", "good@sa.com")

        fake_claims = {"iss": "accounts.google.com", "email": "evil@sa.com"}
        with (
            patch("api.addon.auth.id_token.verify_token", return_value=fake_claims),
            pytest.raises(HTTPException) as exc_info,
        ):
            await verify_google_addon_token(
                request=_mock_request(), authorization="Bearer valid.token.here"
            )
        assert exc_info.value.status_code == 401

    async def test_valid_token_returns_claims(self, monkeypatch):
        monkeypatch.setattr("api.addon.auth.GOOGLE_ADDON_SA_EMAIL", "addons-123@gcp-sa.com")

        fake_claims = {"iss": "accounts.google.com", "email": "addons-123@gcp-sa.com"}
        with patch("api.addon.auth.id_token.verify_token", return_value=fake_claims):
            result = await verify_google_addon_token(
                request=_mock_request(), authorization="Bearer valid.token.here"
            )
        assert result["iss"] == "accounts.google.com"
        assert result["email"] == "addons-123@gcp-sa.com"
