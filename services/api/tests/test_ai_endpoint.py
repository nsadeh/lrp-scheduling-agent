"""Tests for the typed LLM endpoint factory."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langfuse.model import ChatPromptClient
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


def _make_chat_prompt(config: dict | None = None):
    """Create a mock ChatPromptClient that compiles to a message list."""
    prompt = MagicMock(spec=ChatPromptClient)
    prompt.config = config or {
        "model": "claude-sonnet-4-20250514",
        "temperature": 0.1,
        "max_tokens": 2048,
    }
    prompt.is_fallback = False
    prompt.compile.return_value = [
        {"role": "system", "content": "Classify this email."},
        {"role": "user", "content": "Subject: {{subject}}\nBody: {{body}}"},
    ]
    return prompt


def _make_text_prompt(config: dict | None = None):
    """Create a mock TextPromptClient that compiles to a string."""
    prompt = MagicMock()
    # Not a ChatPromptClient instance — endpoint treats it as text
    prompt.__class__ = type("TextPromptClient", (), {})
    prompt.config = config or {
        "model": "claude-sonnet-4-20250514",
        "temperature": 0.1,
        "max_tokens": 2048,
    }
    prompt.is_fallback = False
    prompt.compile.return_value = "Classify this email about {{subject}}"
    return prompt


@pytest.fixture
def mock_langfuse():
    client = MagicMock()
    client.get_prompt.return_value = _make_chat_prompt()
    # Mock the context manager for start_as_current_observation
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    client.start_as_current_observation.return_value = ctx
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

    async def test_chat_prompt_injects_json_schema_into_system(
        self, endpoint, mock_langfuse, mock_llm
    ):
        """Chat prompts get JSON schema injected into the system message."""
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
        # System message should have JSON schema prepended
        system_msg = messages[0]["content"]
        assert "JSON" in system_msg
        assert "classification" in system_msg
        # Original system content should still be there
        assert "Classify this email" in system_msg
        # User message from chat prompt should be preserved
        assert messages[1]["role"] == "user"

    async def test_text_prompt_builds_system_plus_user(self, mock_llm):
        """Text prompts build system (schema + prompt) + user (input JSON)."""
        # Set up mock langfuse with a text prompt
        mock_langfuse = MagicMock()
        mock_langfuse.get_prompt.return_value = _make_text_prompt()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_langfuse.start_as_current_observation.return_value = ctx

        mock_llm.complete.return_value = LLMResponse(
            content=json.dumps({"classification": "test", "confidence": 0.5}),
            model="test",
            provider="test",
        )

        endpoint = llm_endpoint(
            name="text_test",
            prompt_name="text-prompt",
            input_type=SampleInput,
            output_type=SampleOutput,
        )

        await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
        )

        call_kwargs = mock_llm.complete.call_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert "JSON" in messages[0]["content"]
        # User message should be JSON-dumped input
        assert messages[1]["role"] == "user"
        assert "Test" in messages[1]["content"]

    async def test_creates_span_with_endpoint_name(self, endpoint, mock_langfuse, mock_llm):
        """Verify the span is created with the endpoint name, not __call__."""
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

        mock_langfuse.start_as_current_observation.assert_called_once()
        call_kwargs = mock_langfuse.start_as_current_observation.call_args.kwargs
        assert call_kwargs["name"] == "classify_email"

    async def test_observation_output_is_set(self, endpoint, mock_langfuse, mock_llm):
        """Verify the parsed output is recorded on the LangFuse observation."""
        mock_llm.complete.return_value = LLMResponse(
            content=json.dumps({"classification": "scheduling", "confidence": 0.95}),
            model="test",
            provider="test",
        )

        await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
        )

        mock_langfuse.update_current_span.assert_called_once_with(
            output={"classification": "scheduling", "confidence": 0.95},
        )

    async def test_observation_output_set_on_retry(self, endpoint, mock_langfuse, mock_llm):
        """Verify the output is recorded even when the first parse fails and retry succeeds."""
        mock_llm.complete.side_effect = [
            LLMResponse(content="not json", model="test", provider="test"),
            LLMResponse(
                content=json.dumps({"classification": "other", "confidence": 0.7}),
                model="test",
                provider="test",
            ),
        ]

        await endpoint(
            llm=mock_llm,
            langfuse=mock_langfuse,
            data=SampleInput(subject="Test", body="Test"),
        )

        mock_langfuse.update_current_span.assert_called_once_with(
            output={"classification": "other", "confidence": 0.7},
        )
