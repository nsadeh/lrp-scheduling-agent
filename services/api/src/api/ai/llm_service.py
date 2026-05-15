"""LLM service: OpenRouter gateway with multi-model failover.

All LLM calls go through OpenRouter via the OpenAI-compatible API. Model
strings are passed verbatim to OpenRouter — whatever LangFuse (or the
LLM_*_MODEL env vars) hold is what gets sent. Use OpenRouter's
fully-qualified model IDs, e.g. ``anthropic/claude-sonnet-4.6``,
``openai/gpt-4o``, ``google/gemini-2.5-flash``. See
https://openrouter.ai/models for the canonical list.

Latency budget:
- Primary attempt: 25s timeout, 1 in-loop retry
- Secondary attempt: 20s timeout, 0 retries
- Tertiary attempt: 20s timeout, 0 retries
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import sentry_sdk
from openai import AsyncOpenAI

from api.ai.errors import LLMBudgetExceededError, LLMUnavailableError

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Default model when LangFuse prompt config doesn't specify one.
DEFAULT_MODEL = os.environ.get("LLM_DEFAULT_MODEL", "google/gemini-2.5-flash")


@dataclass
class LLMResponse:
    """Normalized response from an LLM call.

    ``finish_reason`` is captured for diagnostic purposes — without it,
    parse failures on the consumer side can't distinguish "natural stop"
    from "length truncation" from "content filter," all of which surface
    as the same parse error at the agent layer.
    """

    content: str
    model: str
    provider: str
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0

    def to_diagnostics(self, *, attempt: int) -> dict:
        """Distill this response into a JSON-friendly diagnostic dict.

        These end up on LangFuse spans and Sentry error contexts, so they
        have to round-trip through JSON cleanly. Keep the shape stable —
        LangFuse and Sentry dashboards may filter on these keys.

        ``attempt`` is 0 for the initial call, 1 for the first retry, etc.
        """
        return {
            "attempt": attempt,
            "finish_reason": self.finish_reason,
            "model": self.model,
            "provider": self.provider,
            "latency_ms": round(self.latency_ms, 1),
            "prompt_tokens": self.usage.get("prompt_tokens"),
            "completion_tokens": self.usage.get("completion_tokens"),
            "total_tokens": self.usage.get("total_tokens"),
            # Surfaces zero-thinking calls in traces — a Gemini endpoint that
            # answered with ~0 reasoning tokens is the failure mode #88's
            # explicit thinking budget guards against.
            "reasoning_tokens": self.usage.get("reasoning_tokens"),
            "content_length_chars": len(self.content or ""),
        }


def init_llm_service() -> "LLMService":
    """Create an LLMService configured against OpenRouter.

    Required env: ``OPENROUTER_API_KEY``.
    Optional env: ``LLM_SECONDARY_MODEL``, ``LLM_TERTIARY_MODEL``.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY must be set. "
            "The AI classification pipeline requires an LLM provider."
        )

    secondary = os.environ.get("LLM_SECONDARY_MODEL", "openai/gpt-4o")
    tertiary = os.environ.get("LLM_TERTIARY_MODEL", "google/gemini-2.5-flash")

    return LLMService(
        api_key=api_key,
        secondary_model=secondary,
        tertiary_model=tertiary,
    )


def _provider_from_model(model: str) -> str:
    """Best-effort provider tag for logging/tracing — first segment of the model ID."""
    if "/" in model:
        return model.split("/", 1)[0]
    return "openrouter"


class LLMService:
    """OpenRouter-backed LLM service with primary→secondary→tertiary failover.

    Calls OpenRouter via the OpenAI async client. The model string is sent
    verbatim — no in-code transformation. Configure full OpenRouter model
    IDs in LangFuse prompt configs and the LLM_*_MODEL env vars.
    """

    def __init__(
        self,
        *,
        api_key: str,
        secondary_model: str = "openai/gpt-4o",
        tertiary_model: str = "google/gemini-2.5-flash",
    ):
        self._client = AsyncOpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
        )
        self._secondary_model = secondary_model
        self._tertiary_model = tertiary_model

        logger.info("LLMService initialized via OpenRouter gateway")

    def _build_call_chain(self, model: str) -> list[str]:
        """Build the ordered failover chain: primary → secondary → tertiary, deduped."""
        candidates = [model, self._secondary_model, self._tertiary_model]
        seen: set[str] = set()
        chain: list[str] = []
        for candidate in candidates:
            if candidate not in seen:
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
        reasoning_max_tokens: int | None = None,
    ) -> LLMResponse:
        """Make an LLM completion call with automatic failover.

        Args:
            messages: OpenAI-format messages list (role + content dicts).
            model: OpenRouter model ID (e.g. ``anthropic/claude-sonnet-4.6``).
                If None, uses LLM_DEFAULT_MODEL. On failure, falls back to
                LLM_SECONDARY_MODEL, then LLM_TERTIARY_MODEL.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens in the response. Note: for thinking
                models, reasoning tokens are drawn from this same budget, so
                it must cover reasoning_max_tokens plus the visible output.
            response_format: Optional response format spec.
            reasoning_max_tokens: If set, request an explicit thinking budget
                (OpenRouter ``reasoning.max_tokens``) and require an endpoint
                that actually supports it. Only applied to the primary model
                leg — see the call-site comment for why failover legs are
                left unconstrained.

        Returns:
            LLMResponse with the completion content and metadata.

        Raises:
            LLMUnavailableError: All models in the chain failed.
            LLMBudgetExceededError: Token budget or spend limit exceeded.
        """
        resolved_model = model or DEFAULT_MODEL
        chain = self._build_call_chain(resolved_model)
        errors: list[str] = []

        for i, candidate_model in enumerate(chain):
            timeout = 25.0 if i == 0 else 20.0
            max_attempts = 2 if i == 0 else 1

            kwargs: dict[str, Any] = {
                "model": candidate_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
            if response_format:
                kwargs["response_format"] = response_format

            # Reasoning + require_parameters only on the primary leg (i == 0).
            # The failover chain dedups to [primary-gemini, openai/gpt-4o];
            # gpt-4o is not reasoning-capable, so sending `reasoning` with
            # require_parameters=true would make OpenRouter exclude every
            # gpt-4o endpoint and silently kill the cross-vendor fallback.
            # Failover is best-effort — run it as a plain call.
            if reasoning_max_tokens is not None and i == 0:
                kwargs["extra_body"] = {
                    "reasoning": {"max_tokens": reasoning_max_tokens},
                    "provider": {"require_parameters": True},
                }

            attempt_error: Exception | None = None
            elapsed_ms: float = 0.0
            response: Any = None

            for attempt in range(max_attempts):
                start = time.monotonic()
                with sentry_sdk.start_span(op="ai.llm", name=candidate_model) as span:
                    span.set_data("ai.attempt", i)
                    span.set_data("ai.is_failover", i > 0)
                    span.set_data("ai.retry", attempt)
                    try:
                        response = await self._client.chat.completions.create(**kwargs)
                    except Exception as exc:
                        elapsed_ms = (time.monotonic() - start) * 1000
                        span.set_data("ai.latency_ms", elapsed_ms)
                        span.set_status("internal_error")
                        attempt_error = exc
                        error_msg = str(exc).lower()

                        if "budget" in error_msg or "spend" in error_msg:
                            raise LLMBudgetExceededError(str(exc)) from exc

                        # Retry within the same model if attempts remain
                        if attempt + 1 < max_attempts:
                            logger.warning(
                                "Model '%s' attempt %d failed (%.0fms), retrying: %s",
                                candidate_model,
                                attempt,
                                elapsed_ms,
                                exc,
                            )
                            continue
                        break

                    elapsed_ms = (time.monotonic() - start) * 1000
                    span.set_data("ai.latency_ms", elapsed_ms)
                    if response.usage:
                        span.set_data("ai.prompt_tokens", response.usage.prompt_tokens or 0)
                        span.set_data("ai.completion_tokens", response.usage.completion_tokens or 0)
                    attempt_error = None
                    break

            if attempt_error is not None:
                errors.append(f"{candidate_model}: {attempt_error} ({elapsed_ms:.0f}ms)")
                if i < len(chain) - 1:
                    logger.warning(
                        "Model '%s' failed (%.0fms), failing over: %s",
                        candidate_model,
                        elapsed_ms,
                        attempt_error,
                    )
                continue

            if i > 0:
                logger.info(
                    "Failover succeeded: '%s' responded in %.0fms (prior errors: %s)",
                    candidate_model,
                    elapsed_ms,
                    "; ".join(errors),
                )

            choice = response.choices[0]
            content = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)

            usage = {}
            if response.usage:
                # reasoning_tokens lives in the OpenAI-compatible
                # completion_tokens_details sub-object. Not every provider
                # populates it (it's a real cross-provider boundary, not a
                # defensive-for-impossible case), so read it None-safe. A
                # value of 0 means the model answered with no thinking budget
                # — a load-bearing signal for trace diagnostics.
                details = getattr(response.usage, "completion_tokens_details", None)
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens or 0,
                    "completion_tokens": response.usage.completion_tokens or 0,
                    "total_tokens": response.usage.total_tokens or 0,
                    "reasoning_tokens": getattr(details, "reasoning_tokens", None) or 0,
                }

            return LLMResponse(
                content=content,
                model=response.model or candidate_model,
                provider=_provider_from_model(response.model or candidate_model),
                finish_reason=finish_reason,
                usage=usage,
                latency_ms=elapsed_ms,
            )

        raise LLMUnavailableError(
            f"All LLM providers failed for model '{resolved_model}': " + "; ".join(errors)
        )
