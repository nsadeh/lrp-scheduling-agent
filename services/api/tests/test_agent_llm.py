"""Tests for the LLM abstraction layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.agent.llm import (
    AnthropicProvider,
    LLMResponse,
    LLMRouter,
    OpenAIProvider,
)


class TestLLMResponse:
    def test_fields(self):
        r = LLMResponse(
            content="hello",
            model="test-model",
            input_tokens=10,
            output_tokens=5,
            latency_ms=123.4,
        )
        assert r.content == "hello"
        assert r.model == "test-model"
        assert r.input_tokens == 10
        assert r.output_tokens == 5
        assert r.latency_ms == 123.4

    def test_frozen(self):
        r = LLMResponse(
            content="hello",
            model="test-model",
            input_tokens=10,
            output_tokens=5,
            latency_ms=100.0,
        )
        with pytest.raises(AttributeError):
            r.content = "changed"


class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_calls_anthropic_api(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="response text")]
        mock_response.model = "claude-sonnet-4-20250514"
        mock_response.usage = MagicMock(input_tokens=50, output_tokens=20)

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("api.agent.llm.anthropic.AsyncAnthropic", return_value=mock_client):
            provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")

        provider._client = mock_client

        result = await provider.complete(
            system="You are helpful.",
            user="Hello",
            max_tokens=512,
            temperature=0.1,
        )

        mock_client.messages.create.assert_called_once_with(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            temperature=0.1,
            system="You are helpful.",
            messages=[{"role": "user", "content": "Hello"}],
        )
        assert result.content == "response text"
        assert result.model == "claude-sonnet-4-20250514"
        assert result.input_tokens == 50
        assert result.output_tokens == 20
        assert result.latency_ms >= 0


class TestOpenAIProvider:
    @pytest.mark.asyncio
    async def test_calls_openai_api(self):
        mock_choice = MagicMock()
        mock_choice.message.content = "openai response"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o"
        mock_response.usage = MagicMock(prompt_tokens=30, completion_tokens=15)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch("api.agent.llm.openai.AsyncOpenAI", return_value=mock_client):
            provider = OpenAIProvider(model="gpt-4o", api_key="test-key")

        provider._client = mock_client

        result = await provider.complete(
            system="You are helpful.",
            user="Hello",
            max_tokens=256,
            temperature=0.5,
        )

        mock_client.chat.completions.create.assert_called_once_with(
            model="gpt-4o",
            max_tokens=256,
            temperature=0.5,
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ],
        )
        assert result.content == "openai response"
        assert result.model == "gpt-4o"
        assert result.input_tokens == 30
        assert result.output_tokens == 15


class TestLLMRouter:
    @pytest.mark.asyncio
    async def test_uses_primary_on_success(self):
        expected = LLMResponse(
            content="primary", model="m1", input_tokens=1, output_tokens=1, latency_ms=10.0
        )
        primary = AsyncMock(spec=["complete"])
        primary.complete = AsyncMock(return_value=expected)
        fallback = AsyncMock(spec=["complete"])
        fallback.complete = AsyncMock()

        router = LLMRouter(primary=primary, fallback=fallback)
        result = await router.complete("sys", "usr")

        assert result.content == "primary"
        primary.complete.assert_called_once()
        fallback.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_on_primary_failure(self):
        expected = LLMResponse(
            content="fallback", model="m2", input_tokens=2, output_tokens=2, latency_ms=20.0
        )
        primary = AsyncMock(spec=["complete"])
        primary.complete = AsyncMock(side_effect=RuntimeError("API down"))
        fallback = AsyncMock(spec=["complete"])
        fallback.complete = AsyncMock(return_value=expected)

        router = LLMRouter(primary=primary, fallback=fallback)
        result = await router.complete("sys", "usr")

        assert result.content == "fallback"
        primary.complete.assert_called_once()
        fallback.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_when_no_fallback(self):
        primary = AsyncMock(spec=["complete"])
        primary.complete = AsyncMock(side_effect=RuntimeError("API down"))

        router = LLMRouter(primary=primary, fallback=None)

        with pytest.raises(RuntimeError, match="API down"):
            await router.complete("sys", "usr")

    @pytest.mark.asyncio
    async def test_raises_when_both_fail(self):
        primary = AsyncMock(spec=["complete"])
        primary.complete = AsyncMock(side_effect=RuntimeError("primary down"))
        fallback = AsyncMock(spec=["complete"])
        fallback.complete = AsyncMock(side_effect=RuntimeError("fallback down"))

        router = LLMRouter(primary=primary, fallback=fallback)

        with pytest.raises(RuntimeError, match="fallback down"):
            await router.complete("sys", "usr")
