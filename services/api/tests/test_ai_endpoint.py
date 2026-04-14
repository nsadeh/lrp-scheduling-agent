"""Tests for the typed LLM endpoint factory."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from api.ai.endpoint import LLMEndpoint, llm_endpoint
from api.ai.errors import LLMParseError
from api.ai.llm_service import LLMResponse


class SampleInput(BaseModel):
    subject: str
    body: str


class SampleOutput(BaseModel):
    classification: str
    confidence: float


@pytest.fixture
def mock_langfuse():
    client = MagicMock()
    prompt = MagicMock()
    prompt.config = {"model": "claude-sonnet-4-20250514", "temperature": 0.1, "max_tokens": 2048}
    prompt.is_fallback = False
    prompt.compile.return_value = "Classify this email about {{subject}}"
    client.get_prompt.return_value = prompt
    return client


@pytest.fixture
def mock_llm():
    service = MagicMock()
    service.complete = AsyncMock()
    return service


class TestLlmEndpointFactory:
    def test_creates_endpoint_instance(self):
        endpoint = llm_endpoint(
            name="test",
            prompt_name="test-prompt",
            input_type=SampleInput,
            output_type=SampleOutput,
        )
        assert isinstance(endpoint, LLMEndpoint)
        assert endpoint.name == "test"
        assert endpoint.prompt_name == "test-prompt"


class TestLLMEndpointCall:
    @pytest.fixture
    def endpoint(self):
        return llm_endpoint(
            name="classify_email",
            prompt_name="scheduling-classify-email",
            input_type=SampleInput,
            output_type=SampleOutput,
        )

    async def test_successful_call_returns_parsed_output(self, endpoint, mock_langfuse, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content=json.dumps({"classification": "scheduling", "confidence": 0.95}),
            model="claude-sonnet-4-20250514",
            provider="anthropic",
            usage={"total_tokens": 100},
            latency_ms=500,
        )

        result = await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Interview", body="Schedule please"),
        )

        assert isinstance(result, SampleOutput)
        assert result.classification == "scheduling"
        assert result.confidence == 0.95

    async def test_strips_markdown_fences(self, endpoint, mock_langfuse, mock_llm):
        fenced = '```json\n{"classification": "scheduling", "confidence": 0.9}\n```'
        mock_llm.complete.return_value = LLMResponse(
            content=fenced,
            model="test",
            provider="test",
        )

        result = await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
        )

        assert result.classification == "scheduling"

    async def test_retries_on_parse_failure(self, endpoint, mock_langfuse, mock_llm):
        # First call returns invalid JSON, second call returns valid JSON
        mock_llm.complete.side_effect = [
            LLMResponse(content="not json at all", model="test", provider="test"),
            LLMResponse(
                content=json.dumps({"classification": "other", "confidence": 0.7}),
                model="test",
                provider="test",
            ),
        ]

        result = await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
        )

        assert result.classification == "other"
        assert mock_llm.complete.call_count == 2

    async def test_raises_parse_error_after_retry_fails(self, endpoint, mock_langfuse, mock_llm):
        mock_llm.complete.side_effect = [
            LLMResponse(content="bad json 1", model="test", provider="test"),
            LLMResponse(content="bad json 2", model="test", provider="test"),
        ]

        with pytest.raises(LLMParseError, match="failed to parse"):
            await endpoint(
                llm=mock_llm,
                langfuse=mock_langfuse,
                data=SampleInput(subject="Test", body="Test"),
            )

    async def test_uses_config_from_langfuse_prompt(self, endpoint, mock_langfuse, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content=json.dumps({"classification": "test", "confidence": 0.5}),
            model="test",
            provider="test",
        )

        await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
        )

        call_kwargs = mock_llm.complete.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"
        assert call_kwargs["temperature"] == 0.1
        assert call_kwargs["max_tokens"] == 2048

    async def test_overrides_take_precedence(self, endpoint, mock_langfuse, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content=json.dumps({"classification": "test", "confidence": 0.5}),
            model="test",
            provider="test",
        )

        await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
            model="gpt-4o",
            temperature=0.9,
        )

        call_kwargs = mock_llm.complete.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["temperature"] == 0.9

    async def test_messages_include_json_schema(self, endpoint, mock_langfuse, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content=json.dumps({"classification": "test", "confidence": 0.5}),
            model="test",
            provider="test",
        )

        await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
        )

        call_kwargs = mock_llm.complete.call_args.kwargs
        messages = call_kwargs["messages"]
        system_msg = messages[0]["content"]
        assert "classification" in system_msg
        assert "confidence" in system_msg
        assert "JSON" in system_msg
