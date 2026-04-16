"""Tests for Google Workspace Add-on request verification."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from api.addon.auth import verify_google_addon_token


def _mock_request(url: str = "https://example.com/addon/homepage") -> MagicMock:
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

    async def test_valid_token_returns_claims(self):
        fake_claims = {"iss": "accounts.google.com", "aud": "https://example.com/addon/homepage"}
        with patch("api.addon.auth.id_token.verify_token", return_value=fake_claims):
            result = await verify_google_addon_token(
                request=_mock_request(), authorization="Bearer valid.token.here"
            )
        assert result == fake_claims

    async def test_verifies_with_request_url_as_audience(self):
        url = "https://my-app.ngrok-free.app/addon/homepage"
        fake_claims = {"iss": "accounts.google.com"}
        with patch("api.addon.auth.id_token.verify_token", return_value=fake_claims) as mock_verify:
            await verify_google_addon_token(
                request=_mock_request(url=url), authorization="Bearer valid.token.here"
            )
        # Verify the audience was set to the request URL
        mock_verify.assert_called_once()
        call_kwargs = mock_verify.call_args
        assert call_kwargs[1]["audience"] == url
