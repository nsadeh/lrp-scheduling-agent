"""Tests for LLMService initialization, completion, and failover."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.ai.errors import LLMBudgetExceededError, LLMUnavailableError
from api.ai.llm_service import (
    LLMService,
    _provider_from_model,
    init_llm_service,
)


class TestInitLlmService:
    def test_raises_when_openrouter_key_missing(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"),
        ):
            init_llm_service()

    def test_returns_service_with_openrouter_key(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-test"}, clear=True):
            service = init_llm_service()
            assert isinstance(service, LLMService)

    def test_uses_failover_env_overrides(self):
        env = {
            "OPENROUTER_API_KEY": "sk-or-test",
            "LLM_SECONDARY_MODEL": "openai/gpt-4o-mini",
            "LLM_TERTIARY_MODEL": "anthropic/claude-haiku-4.5",
        }
        with patch.dict("os.environ", env, clear=True):
            service = init_llm_service()
            assert service._secondary_model == "openai/gpt-4o-mini"
            assert service._tertiary_model == "anthropic/claude-haiku-4.5"


class TestProviderFromModel:
    def test_extracts_first_segment(self):
        assert _provider_from_model("anthropic/claude-sonnet-4.6") == "anthropic"
        assert _provider_from_model("openai/gpt-4o") == "openai"
        assert _provider_from_model("google/gemini-2.5-flash") == "google"

    def test_no_slash_falls_back_to_openrouter(self):
        assert _provider_from_model("some-bare-name") == "openrouter"


class TestBuildCallChain:
    def test_primary_plus_failovers(self):
        service = LLMService(
            api_key="sk-or-test",
            secondary_model="openai/gpt-4o",
            tertiary_model="google/gemini-2.5-flash",
        )
        chain = service._build_call_chain("anthropic/claude-sonnet-4.6")
        assert chain == [
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-4o",
            "google/gemini-2.5-flash",
        ]

    def test_deduplicates_when_primary_matches_secondary(self):
        service = LLMService(
            api_key="sk-or-test",
            secondary_model="openai/gpt-4o",
            tertiary_model="google/gemini-2.5-flash",
        )
        chain = service._build_call_chain("openai/gpt-4o")
        assert chain == ["openai/gpt-4o", "google/gemini-2.5-flash"]

    def test_deduplicates_when_secondary_matches_tertiary(self):
        service = LLMService(
            api_key="sk-or-test",
            secondary_model="openai/gpt-4o",
            tertiary_model="openai/gpt-4o",
        )
        chain = service._build_call_chain("anthropic/claude-sonnet-4.6")
        assert chain == ["anthropic/claude-sonnet-4.6", "openai/gpt-4o"]

    def test_passes_through_verbatim(self):
        # No transformation — whatever string was provided is what's in the chain.
        service = LLMService(api_key="sk-or-test")
        chain = service._build_call_chain("custom/some-experimental-model")
        assert chain[0] == "custom/some-experimental-model"


class TestLLMServiceComplete:
    @pytest.fixture
    def service(self):
        return LLMService(
            api_key="sk-or-test",
            secondary_model="openai/gpt-4o",
            tertiary_model="google/gemini-2.5-flash",
        )

    def _mock_response(self, content="Hello", model="anthropic/claude-sonnet-4.6"):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        resp.model = model
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return resp

    async def test_complete_returns_llm_response(self, service):
        service._client.chat.completions.create = AsyncMock(return_value=self._mock_response())

        result = await service.complete(
            messages=[{"role": "user", "content": "hello"}],
        )

        assert result.content == "Hello"
        assert result.usage["total_tokens"] == 15
        assert result.latency_ms > 0
        assert result.provider == "anthropic"
        assert result.model == "anthropic/claude-sonnet-4.6"

    async def test_complete_passes_model_verbatim(self, service):
        service._client.chat.completions.create = AsyncMock(
            return_value=self._mock_response(model="openai/gpt-4o")
        )

        await service.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-4o",
        )

        call_kwargs = service._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "openai/gpt-4o"

    async def test_failover_on_primary_failure(self, service):
        service._client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("openrouter 500 server error"),
                Exception("openrouter 500 server error"),  # primary retry
                self._mock_response(content="Fallback response", model="openai/gpt-4o"),
            ]
        )

        result = await service.complete(
            messages=[{"role": "user", "content": "hello"}],
            model="anthropic/claude-sonnet-4.6",
        )

        assert result.content == "Fallback response"
        # Primary attempts twice (1 retry), secondary succeeds on first try
        assert service._client.chat.completions.create.call_count == 3

    async def test_raises_llm_unavailable_when_all_fail(self, service):
        service._client.chat.completions.create = AsyncMock(
            side_effect=Exception("all providers failed")
        )

        with pytest.raises(LLMUnavailableError, match="All LLM providers failed"):
            await service.complete(
                messages=[{"role": "user", "content": "hello"}],
            )

    async def test_raises_budget_exceeded(self, service):
        service._client.chat.completions.create = AsyncMock(
            side_effect=Exception("budget limit exceeded")
        )

        with pytest.raises(LLMBudgetExceededError):
            await service.complete(
                messages=[{"role": "user", "content": "hello"}],
            )

    async def test_bad_request_fails_over(self, service):
        # OpenRouter returns 400 for invalid model IDs — failover should kick in
        # so a misconfigured primary doesn't take down the whole call.
        service._client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("400 not a valid model ID"),
                Exception("400 not a valid model ID"),  # primary retry
                self._mock_response(content="from secondary", model="openai/gpt-4o"),
            ]
        )

        result = await service.complete(
            messages=[{"role": "user", "content": "hello"}],
            model="anthropic/typo-here",
        )

        assert result.content == "from secondary"

    async def test_complete_handles_none_usage(self, service):
        resp = self._mock_response()
        resp.usage = None
        service._client.chat.completions.create = AsyncMock(return_value=resp)

        result = await service.complete(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.usage == {}

    async def test_passes_temperature_and_max_tokens(self, service):
        service._client.chat.completions.create = AsyncMock(return_value=self._mock_response())

        await service.complete(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            max_tokens=2000,
        )

        call_kwargs = service._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 2000

    async def test_primary_timeout_is_25s(self, service):
        service._client.chat.completions.create = AsyncMock(return_value=self._mock_response())

        await service.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/claude-sonnet-4.6",
        )

        call_kwargs = service._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["timeout"] == 25.0
