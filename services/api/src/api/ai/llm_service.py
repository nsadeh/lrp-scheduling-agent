"""LLM service with multi-provider failover.

Uses litellm.acompletion() directly (not the Router) to support caller-specified
models from LangFuse prompt config. Implements failover via a fallback model map:
when the primary model's provider fails, the service tries equivalent-tier models
on other providers.

Latency budget:
- Primary attempt: 5s timeout, 1 retry (10s max)
- Secondary attempt: 4s timeout, 0 retries
- Tertiary attempt: 4s timeout, 0 retries
- Total budget: ~18s max wall-clock time
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import litellm

from api.ai.errors import LLMBudgetExceededError, LLMUnavailableError

logger = logging.getLogger(__name__)

# Silence litellm's verbose logging
litellm.suppress_debug_info = True

# Default model when LangFuse prompt config doesn't specify one
DEFAULT_MODEL = "claude-sonnet-4-20250514"

# Provider prefix → env var key name (for setting API keys)
_PROVIDER_PREFIX_MAP = {
    "anthropic/": "anthropic",
    "openai/": "openai",
    "gemini/": "google",
}

# Fallback model mapping: when the requested model's provider is down,
# try equivalent-tier models on other providers. Each entry is an ordered
# list of (provider_prefixed_model) to try.
FALLBACK_MODEL_MAP: dict[str, list[str]] = {
    # Claude Sonnet/Opus tier
    "claude-sonnet-4-20250514": ["openai/gpt-4o", "gemini/gemini-2.5-pro"],
    "claude-opus-4-20250514": ["openai/gpt-4o", "gemini/gemini-2.5-pro"],
    # Claude Haiku tier
    "claude-haiku-4-5-20251001": ["openai/gpt-4o-mini", "gemini/gemini-2.0-flash"],
    # OpenAI models
    "gpt-4o": ["anthropic/claude-sonnet-4-20250514", "gemini/gemini-2.5-pro"],
    "gpt-4o-mini": ["anthropic/claude-haiku-4-5-20251001", "gemini/gemini-2.0-flash"],
    # Gemini models
    "gemini-2.5-pro": ["anthropic/claude-sonnet-4-20250514", "openai/gpt-4o"],
    "gemini-2.0-flash": ["anthropic/claude-haiku-4-5-20251001", "openai/gpt-4o-mini"],
}


def _resolve_provider_model(model: str) -> str:
    """Add provider prefix if not already present.

    LiteLLM requires provider-prefixed model names (e.g., "anthropic/claude-sonnet-4-20250514").
    If the model name doesn't have a prefix, infer the provider from the model name.
    """
    if "/" in model:
        return model

    # Infer provider from model name prefix
    if model.startswith("claude"):
        return f"anthropic/{model}"
    if model.startswith("gpt"):
        return f"openai/{model}"
    if model.startswith("gemini"):
        return f"gemini/{model}"

    # Unknown model — pass through and let LiteLLM resolve it
    return model


@dataclass
class LLMResponse:
    """Normalized response from an LLM call."""

    content: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0


def init_llm_service() -> "LLMService | None":
    """Create an LLMService if at least one provider key is configured."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    google_key = os.environ.get("GOOGLE_AI_API_KEY")

    if not any([anthropic_key, openai_key, google_key]):
        logger.warning("No LLM provider API keys set — LLMService disabled")
        return None

    return LLMService(
        anthropic_key=anthropic_key,
        openai_key=openai_key,
        google_key=google_key,
    )


class LLMService:
    """Multi-provider LLM service with automatic failover and bounded latency.

    Calls litellm.acompletion() directly with provider-prefixed model names.
    When a call fails, tries equivalent-tier fallback models on other providers.
    """

    def __init__(
        self,
        anthropic_key: str | None = None,
        openai_key: str | None = None,
        google_key: str | None = None,
    ):
        self._api_keys: dict[str, str] = {}
        if anthropic_key:
            self._api_keys["anthropic"] = anthropic_key
        if openai_key:
            self._api_keys["openai"] = openai_key
        if google_key:
            self._api_keys["google"] = google_key

        if not self._api_keys:
            raise ValueError("At least one LLM provider API key must be set")

        providers = [p.capitalize() for p in self._api_keys]
        logger.info("LLMService initialized with providers: %s", ", ".join(providers))

    def _get_api_key_for_model(self, prefixed_model: str) -> str | None:
        """Get the API key for a provider-prefixed model name."""
        for prefix, provider in _PROVIDER_PREFIX_MAP.items():
            if prefixed_model.startswith(prefix):
                return self._api_keys.get(provider)
        return None

    def _build_call_chain(self, model: str) -> list[str]:
        """Build ordered list of models to try: primary + fallbacks with valid keys."""
        prefixed = _resolve_provider_model(model)
        chain = [prefixed]

        # Strip prefix to look up fallbacks
        bare_model = model.split("/")[-1] if "/" in model else model
        fallbacks = FALLBACK_MODEL_MAP.get(bare_model, [])

        for fb in fallbacks:
            if self._get_api_key_for_model(fb) is not None:
                chain.append(fb)

        return chain

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Make an LLM completion call with automatic failover.

        Args:
            messages: OpenAI-format messages list (role + content dicts).
            model: Model name from LangFuse prompt config (e.g.,
                "claude-sonnet-4-20250514" or "anthropic/claude-sonnet-4-20250514").
                Provider is inferred from the name. If None, uses DEFAULT_MODEL.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens in the response.
            response_format: Optional response format spec (e.g., {"type": "json_object"}).

        Returns:
            LLMResponse with the completion content and metadata.

        Raises:
            LLMUnavailableError: All providers failed.
            LLMBudgetExceededError: Token budget or spend limit exceeded.
        """
        resolved_model = model or DEFAULT_MODEL
        chain = self._build_call_chain(resolved_model)
        errors: list[str] = []

        for i, candidate_model in enumerate(chain):
            api_key = self._get_api_key_for_model(candidate_model)
            if api_key is None:
                continue

            # Primary gets 1 retry, fallbacks get 0
            retries = 1 if i == 0 else 0
            timeout = 5.0 if i == 0 else 4.0

            kwargs: dict[str, Any] = {
                "model": candidate_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "api_key": api_key,
                "timeout": timeout,
                "num_retries": retries,
            }
            if response_format:
                kwargs["response_format"] = response_format

            start = time.monotonic()
            try:
                response = await litellm.acompletion(**kwargs)
            except Exception as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                error_msg = str(exc).lower()

                if "budget" in error_msg or "spend" in error_msg:
                    raise LLMBudgetExceededError(str(exc)) from exc

                # 400-level client errors don't failover (caller's fault)
                if "400" in error_msg or "bad request" in error_msg:
                    raise LLMUnavailableError(f"Bad request to '{candidate_model}': {exc}") from exc

                errors.append(f"{candidate_model}: {exc} ({elapsed_ms:.0f}ms)")
                if i < len(chain) - 1:
                    logger.warning(
                        "Provider failed for '%s' (%.0fms), failing over: %s",
                        candidate_model,
                        elapsed_ms,
                        exc,
                    )
                continue

            elapsed_ms = (time.monotonic() - start) * 1000

            # Log if we had to failover
            if i > 0:
                logger.info(
                    "Failover succeeded: '%s' responded in %.0fms (primary errors: %s)",
                    candidate_model,
                    elapsed_ms,
                    "; ".join(errors),
                )

            # Extract response content
            choice = response.choices[0]
            content = choice.message.content or ""

            # Extract usage
            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens or 0,
                    "completion_tokens": response.usage.completion_tokens or 0,
                    "total_tokens": response.usage.total_tokens or 0,
                }

            return LLMResponse(
                content=content,
                model=response.model or candidate_model,
                provider=getattr(response, "_hidden_params", {}).get(
                    "custom_llm_provider", "unknown"
                ),
                usage=usage,
                latency_ms=elapsed_ms,
            )

        raise LLMUnavailableError(
            f"All LLM providers failed for model '{resolved_model}': " + "; ".join(errors)
        )
