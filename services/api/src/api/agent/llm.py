"""LLM abstraction layer with provider routing and fallback."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import anthropic
import openai

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResponse:
    """Standardised response from any LLM provider."""

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class LLMProvider(ABC):
    """Abstract base for LLM providers."""

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send a single-turn completion request."""


class AnthropicProvider(LLMProvider):
    """Provider backed by the Anthropic Messages API."""

    def __init__(self, model: str = "claude-sonnet-4-20250514", api_key: str | None = None) -> None:
        self._model = model
        kwargs = {"api_key": api_key} if api_key else {}
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        start = time.monotonic()
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        latency = (time.monotonic() - start) * 1000

        content = response.content[0].text if response.content else ""
        return LLMResponse(
            content=content,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency,
        )


class OpenAIProvider(LLMProvider):
    """Provider backed by the OpenAI Chat Completions API."""

    def __init__(self, model: str = "gpt-4o", api_key: str | None = None) -> None:
        self._model = model
        self._client = openai.AsyncOpenAI(api_key=api_key) if api_key else openai.AsyncOpenAI()

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        start = time.monotonic()
        response = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        latency = (time.monotonic() - start) * 1000

        choice = response.choices[0] if response.choices else None
        content = choice.message.content or "" if choice else ""
        usage = response.usage
        return LLMResponse(
            content=content,
            model=response.model or self._model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency,
        )


class LLMRouter:
    """Tries a primary provider, falls back to a secondary on failure."""

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        try:
            return await self._primary.complete(
                system, user, max_tokens=max_tokens, temperature=temperature
            )
        except Exception:
            if self._fallback is None:
                raise
            logger.warning(
                "Primary LLM provider failed, falling back to secondary",
                exc_info=True,
            )
            return await self._fallback.complete(
                system, user, max_tokens=max_tokens, temperature=temperature
            )
