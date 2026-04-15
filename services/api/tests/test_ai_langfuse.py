"""Tests for LangFuse client initialization and prompt fetching."""

from unittest.mock import MagicMock, patch

import pytest

from api.ai.errors import LangFuseUnavailableError, PromptNotFoundError
from api.ai.langfuse_client import fetch_prompt, init_langfuse


class TestInitLangfuse:
    def test_returns_none_when_keys_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            assert init_langfuse() is None

    def test_returns_none_when_only_public_key_set(self):
        with patch.dict("os.environ", {"LANGFUSE_PUBLIC_KEY": "pk-123"}, clear=True):
            assert init_langfuse() is None

    def test_returns_client_when_both_keys_set(self):
        env = {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
        }
        with patch.dict("os.environ", env, clear=True):
            client = init_langfuse()
            assert client is not None

    def test_passes_host_when_set(self):
        env = {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            "LANGFUSE_HOST": "https://custom.langfuse.dev",
        }
        with (
            patch("api.ai.langfuse_client.Langfuse") as mock_cls,
            patch.dict("os.environ", env, clear=True),
        ):
            init_langfuse()
            mock_cls.assert_called_once_with(
                public_key="pk-test",
                secret_key="sk-test",
                host="https://custom.langfuse.dev",
            )


class TestFetchPrompt:
    def test_fetches_prompt_by_name(self):
        mock_client = MagicMock()
        mock_prompt = MagicMock()
        mock_prompt.is_fallback = False
        mock_client.get_prompt.return_value = mock_prompt

        result = fetch_prompt(mock_client, "test-prompt")

        assert result is mock_prompt
        mock_client.get_prompt.assert_called_once_with(
            "test-prompt", label="production", type="chat"
        )

    def test_raises_prompt_not_found_error(self):
        mock_client = MagicMock()
        mock_client.get_prompt.side_effect = Exception("Prompt not found in Langfuse")

        with pytest.raises(PromptNotFoundError, match="not found"):
            fetch_prompt(mock_client, "nonexistent")

    def test_raises_langfuse_unavailable_on_connection_error(self):
        mock_client = MagicMock()
        mock_client.get_prompt.side_effect = Exception("Connection refused")

        with pytest.raises(LangFuseUnavailableError, match="Connection refused"):
            fetch_prompt(mock_client, "test-prompt")

    def test_logs_warning_when_serving_fallback(self, caplog):
        mock_client = MagicMock()
        mock_prompt = MagicMock()
        mock_prompt.is_fallback = True
        mock_client.get_prompt.return_value = mock_prompt

        import logging

        with caplog.at_level(logging.WARNING):
            result = fetch_prompt(mock_client, "test-prompt")

        assert result is mock_prompt
        assert "cached/fallback" in caplog.text
