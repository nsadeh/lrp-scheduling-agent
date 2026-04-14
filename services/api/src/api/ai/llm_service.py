"""Multi-provider LLM service built on LiteLLM Router.

Routes calls through Anthropic (primary) → OpenAI (secondary) → Google (tertiary)
with bounded latency, retry, and automatic failover.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from litellm import Router

from api.ai.errors import LLMParseError, LLMUnavailableError

logger = logging.getLogger(__name__)

# Default models per provider
_DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"

# Fallback mappings: when a provider is down, use equivalent on next provider
_FALLBACK_MAP: dict[str, list[str]] = {
    "default": ["default"],
}


def init_llm_service() -> LLMService | None:
    """Initialize the LLM service from environment variables.

    Returns None if no provider keys are configured.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        logger.warning("ANTHROPIC_API_KEY not set — LLM service disabled")
        return None

    model_list = [
        {
            "model_name": "default",
            "litellm_params": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "api_key": anthropic_key,
            },
        },
    ]

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        model_list.append(
            {
                "model_name": "default",
                "litellm_params": {
                    "model": "openai/gpt-4o-mini",
                    "api_key": openai_key,
                },
            }
        )

    google_key = os.environ.get("GOOGLE_AI_API_KEY")
    if google_key:
        model_list.append(
            {
                "model_name": "default",
                "litellm_params": {
                    "model": "gemini/gemini-2.0-flash",
                    "api_key": google_key,
                },
            }
        )

    router = Router(
        model_list=model_list,
        fallbacks=[_FALLBACK_MAP],
        timeout=5,
        num_retries=1,
        retry_after=0,
        allowed_fails=1,
        cooldown_time=60,
    )

    providers = [m["litellm_params"]["model"].split("/")[0] for m in model_list]
    logger.info("LLM service initialized with providers: %s", ", ".join(providers))

    return LLMService(router)


class LLMService:
    """Multi-provider LLM calling with failover and structured output parsing."""

    def __init__(self, router: Router):
        self._router = router

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Call the LLM and return the text response.

        Args:
            messages: Chat messages (system, user, assistant).
            model: Model identifier (e.g. "anthropic/claude-haiku-4-5-20251001").
                   If None, uses the router's default.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in response.
            response_format: Optional response format spec (e.g. {"type": "json_object"}).

        Returns:
            The LLM's text response content.

        Raises:
            LLMUnavailableError: All providers failed.
        """
        kwargs: dict[str, Any] = {
            "model": model or "default",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        try:
            response = await self._router.acompletion(**kwargs)
        except Exception as exc:
            raise LLMUnavailableError(f"All LLM providers failed: {exc}") from exc

        return response.choices[0].message.content

    async def complete_json(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> dict:
        """Call the LLM and parse the response as JSON.

        Retries once with a fix-up message if the first response isn't valid JSON.

        Returns:
            Parsed JSON dict.

        Raises:
            LLMParseError: Response is not valid JSON after retry.
            LLMUnavailableError: All providers failed.
        """
        text = await self.complete(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # Try to parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM returned invalid JSON, retrying with fix-up prompt")

        # Retry with fix-up
        retry_messages = [
            *messages,
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON. "
                    "Please respond with ONLY valid JSON, no markdown fences or extra text."
                ),
            },
        ]

        text = await self.complete(
            messages=retry_messages,
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
        )

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"LLM response is not valid JSON after retry: {exc}") from exc
