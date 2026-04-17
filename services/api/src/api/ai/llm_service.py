"""LLM service with multi-provider failover.

Uses litellm.acompletion() with a simple primary → secondary → tertiary
failover chain configured via environment variables.

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
import sentry_sdk

from api.ai.errors import LLMBudgetExceededError, LLMUnavailableError

logger = logging.getLogger(__name__)

# Silence litellm's verbose logging
litellm.suppress_debug_info = True

# Default model when LangFuse prompt config doesn't specify one
DEFAULT_MODEL = os.environ.get("LLM_DEFAULT_MODEL", "claude-sonnet-4-20250514")


def _resolve_provider_model(model: str) -> str:
    """Add provider prefix if not already present.

    LiteLLM requires provider-prefixed model names (e.g., "anthropic/claude-sonnet-4-20250514").
    If the model name doesn't have a prefix, infer the provider from the model name.
    """
    if "/" in model:
        return model

    if model.startswith("claude"):
        return f"anthropic/{model}"
    if model.startswith("gpt"):
        return f"openai/{model}"
    if model.startswith("gemini"):
        return f"gemini/{model}"

    # Unknown model — pass through and let LiteLLM resolve it
    return model


# Provider prefix → api_keys dict key
_PROVIDER_PREFIX_MAP = {
    "anthropic/": "anthropic",
    "openai/": "openai",
    "gemini/": "google",
}


@dataclass
class LLMResponse:
    """Normalized response from an LLM call."""

    content: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0


def init_llm_service() -> "LLMService | None":
    """Create an LLMService if at least one provider key is configured.

    Failover chain is configured via env vars:
        LLM_SECONDARY_MODEL — model to try when primary fails
        LLM_TERTIARY_MODEL  — model to try when secondary fails
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    google_key = os.environ.get("GOOGLE_AI_API_KEY")

    if not any([anthropic_key, openai_key, google_key]):
        logger.warning("No LLM provider API keys set — LLMService disabled")
        return None

    secondary = os.environ.get("LLM_SECONDARY_MODEL", "gpt-4o")
    tertiary = os.environ.get("LLM_TERTIARY_MODEL", "gemini-2.0-flash")

    return LLMService(
        anthropic_key=anthropic_key,
        openai_key=openai_key,
        google_key=google_key,
        secondary_model=secondary,
        tertiary_model=tertiary,
    )


class LLMService:
    """Multi-provider LLM service with automatic failover and bounded latency.

    Calls litellm.acompletion() directly with provider-prefixed model names.
    When the primary model fails, tries secondary then tertiary models.
    """

    def __init__(
        self,
        anthropic_key: str | None = None,
        openai_key: str | None = None,
        google_key: str | None = None,
        secondary_model: str = "gpt-4o",
        tertiary_model: str = "gemini-2.0-flash",
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

        self._secondary_model = _resolve_provider_model(secondary_model)
        self._tertiary_model = _resolve_provider_model(tertiary_model)

        providers = [p.capitalize() for p in self._api_keys]
        logger.info("LLMService initialized with providers: %s", ", ".join(providers))

    def _get_api_key_for_model(self, prefixed_model: str) -> str | None:
        """Get the API key for a provider-prefixed model name."""
        for prefix, provider in _PROVIDER_PREFIX_MAP.items():
            if prefixed_model.startswith(prefix):
                return self._api_keys.get(provider)
        return None

    def _build_call_chain(self, model: str) -> list[str]:
        """Build ordered list of models to try: primary → secondary → tertiary.

        Only includes models whose provider keys are configured.
        """
        primary = _resolve_provider_model(model)
        candidates = [primary, self._secondary_model, self._tertiary_model]

        # Deduplicate while preserving order, and filter to models with valid keys
        seen: set[str] = set()
        chain: list[str] = []
        for candidate in candidates:
            if candidate not in seen and self._get_api_key_for_model(candidate) is not None:
                seen.add(candidate)
                chain.append(candidate)

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
                "claude-sonnet-4-20250514"). If None, uses LLM_DEFAULT_MODEL.
                On failure, falls back to LLM_SECONDARY_MODEL, then
                LLM_TERTIARY_MODEL.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens in the response.
            response_format: Optional response format spec.

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
            with sentry_sdk.start_span(op="ai.llm", name=candidate_model) as span:
                span.set_data("ai.attempt", i)
                span.set_data("ai.is_failover", i > 0)
                try:
                    response = await litellm.acompletion(**kwargs)
                except Exception as exc:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    span.set_data("ai.latency_ms", elapsed_ms)
                    span.set_status("internal_error")
                    error_msg = str(exc).lower()

                    if "budget" in error_msg or "spend" in error_msg:
                        raise LLMBudgetExceededError(str(exc)) from exc

                    # 400-level client errors don't failover (caller's fault)
                    if "400" in error_msg or "bad request" in error_msg:
                        raise LLMUnavailableError(
                            f"Bad request to '{candidate_model}': {exc}"
                        ) from exc

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
                span.set_data("ai.latency_ms", elapsed_ms)
                if response.usage:
                    span.set_data("ai.prompt_tokens", response.usage.prompt_tokens or 0)
                    span.set_data("ai.completion_tokens", response.usage.completion_tokens or 0)

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
