"""Tests for LLMService initialization, completion, and failover."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.ai.errors import LLMBudgetExceededError, LLMUnavailableError
from api.ai.llm_service import (
    LLMService,
    _resolve_provider_model,
    init_llm_service,
)


class TestInitLlmService:
    def test_returns_none_when_no_keys_set(self):
        with patch.dict("os.environ", {}, clear=True):
            assert init_llm_service() is None

    def test_returns_service_with_anthropic_key_only(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
            service = init_llm_service()
            assert service is not None
            assert isinstance(service, LLMService)

    def test_returns_service_with_all_keys(self):
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-oai-test",
            "GOOGLE_AI_API_KEY": "gai-test",
        }
        with patch.dict("os.environ", env, clear=True):
            service = init_llm_service()
            assert service is not None


class TestResolveProviderModel:
    def test_already_prefixed(self):
        assert (
            _resolve_provider_model("anthropic/claude-sonnet-4-20250514")
            == "anthropic/claude-sonnet-4-20250514"
        )

    def test_claude_model(self):
        assert (
            _resolve_provider_model("claude-sonnet-4-20250514")
            == "anthropic/claude-sonnet-4-20250514"
        )

    def test_gpt_model(self):
        assert _resolve_provider_model("gpt-4o") == "openai/gpt-4o"

    def test_gemini_model(self):
        assert _resolve_provider_model("gemini-2.0-flash") == "gemini/gemini-2.0-flash"

    def test_unknown_model_passthrough(self):
        assert _resolve_provider_model("some-custom-model") == "some-custom-model"


class TestBuildCallChain:
    def test_primary_plus_fallbacks(self):
        service = LLMService(anthropic_key="ak", openai_key="ok", google_key="gk")
        chain = service._build_call_chain("claude-sonnet-4-20250514")
        assert chain[0] == "anthropic/claude-sonnet-4-20250514"
        assert "openai/gpt-4o" in chain
        assert "gemini/gemini-2.5-pro" in chain

    def test_skips_fallbacks_without_keys(self):
        service = LLMService(anthropic_key="ak")
        chain = service._build_call_chain("claude-sonnet-4-20250514")
        assert chain == ["anthropic/claude-sonnet-4-20250514"]

    def test_unknown_model_no_fallbacks(self):
        service = LLMService(anthropic_key="ak")
        chain = service._build_call_chain("some-unknown-model")
        assert chain == ["some-unknown-model"]


class TestLLMServiceComplete:
    @pytest.fixture
    def service(self):
        return LLMService(anthropic_key="sk-ant-test", openai_key="sk-oai-test")

    def _mock_response(self, content="Hello", model="anthropic/claude-sonnet-4-20250514"):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        resp.model = model
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        resp._hidden_params = {"custom_llm_provider": "anthropic"}
        return resp

    @patch("api.ai.llm_service.litellm")
    async def test_complete_returns_llm_response(self, mock_litellm, service):
        mock_litellm.acompletion = AsyncMock(return_value=self._mock_response())

        result = await service.complete(
            messages=[{"role": "user", "content": "hello"}],
        )

        assert result.content == "Hello"
        assert result.usage["total_tokens"] == 15
        assert result.latency_ms > 0

    @patch("api.ai.llm_service.litellm")
    async def test_complete_passes_correct_model(self, mock_litellm, service):
        mock_litellm.acompletion = AsyncMock(return_value=self._mock_response())

        await service.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
        )

        call_kwargs = mock_litellm.acompletion.call_args.kwargs
        assert call_kwargs["model"] == "openai/gpt-4o"

    @patch("api.ai.llm_service.litellm")
    async def test_failover_on_primary_failure(self, mock_litellm, service):
        mock_litellm.acompletion = AsyncMock(
            side_effect=[
                Exception("anthropic 500 server error"),
                self._mock_response(content="Fallback response", model="openai/gpt-4o"),
            ]
        )

        result = await service.complete(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-20250514",
        )

        assert result.content == "Fallback response"
        assert mock_litellm.acompletion.call_count == 2

    @patch("api.ai.llm_service.litellm")
    async def test_raises_llm_unavailable_when_all_fail(self, mock_litellm, service):
        mock_litellm.acompletion = AsyncMock(side_effect=Exception("all providers failed"))

        with pytest.raises(LLMUnavailableError, match="All LLM providers failed"):
            await service.complete(
                messages=[{"role": "user", "content": "hello"}],
            )

    @patch("api.ai.llm_service.litellm")
    async def test_raises_budget_exceeded(self, mock_litellm, service):
        mock_litellm.acompletion = AsyncMock(side_effect=Exception("budget limit exceeded"))

        with pytest.raises(LLMBudgetExceededError):
            await service.complete(
                messages=[{"role": "user", "content": "hello"}],
            )

    @patch("api.ai.llm_service.litellm")
    async def test_bad_request_does_not_failover(self, mock_litellm, service):
        mock_litellm.acompletion = AsyncMock(
            side_effect=Exception("400 bad request: invalid messages format")
        )

        with pytest.raises(LLMUnavailableError, match="Bad request"):
            await service.complete(
                messages=[{"role": "user", "content": "hello"}],
            )

        # Should have called only once — no failover on client error
        assert mock_litellm.acompletion.call_count == 1

    @patch("api.ai.llm_service.litellm")
    async def test_complete_handles_none_usage(self, mock_litellm, service):
        resp = self._mock_response()
        resp.usage = None
        mock_litellm.acompletion = AsyncMock(return_value=resp)

        result = await service.complete(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.usage == {}

    @patch("api.ai.llm_service.litellm")
    async def test_passes_temperature_and_max_tokens(self, mock_litellm, service):
        mock_litellm.acompletion = AsyncMock(return_value=self._mock_response())

        await service.complete(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            max_tokens=2000,
        )

        call_kwargs = mock_litellm.acompletion.call_args.kwargs
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 2000
