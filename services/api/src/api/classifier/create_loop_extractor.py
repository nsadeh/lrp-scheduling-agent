"""On-demand create-loop field extraction (manual path).

Runs against a Gmail thread when the coordinator clicks "Create New Loop"
on a thread the classifier did not flag as CREATE_LOOP. Emits the same
`CreateLoopExtraction` shape the classifier writes into action_data, so
downstream form-prefill code handles both paths uniformly. See
rfcs/rfc-infer-create-loop-fields.md.

I/O contract (prompt v7):
- Input is XML: `<input><coordinator>…</coordinator><current-email>…
  </current-email><thread-history>…</thread-history></input>`.
- Output is a numbered precognition checklist followed by a
  `<loop>{ …single JSON object… }</loop>` envelope.
- A parse failure triggers one conversation-history retry (assistant turn
  + user follow-up) rather than the `LLMEndpoint` JSON-fixup path — the
  endpoint's "respond ONLY with JSON" injection fights v7's reasoning
  preamble + envelope, same conflict as the loop classifier (PR #87).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langfuse.model import ChatPromptClient
from pydantic import BaseModel

from api.ai.langfuse_client import fetch_prompt
from api.ai.llm_service import DEFAULT_MODEL
from api.classifier.agent_runtime import (
    LoopEnvelopeParseError,
    build_error_followup,
    parse_loop_envelope,
)

if TYPE_CHECKING:
    from langfuse import Langfuse

    from api.ai.llm_service import LLMResponse, LLMService
    from api.classifier.models import CreateLoopExtraction

logger = logging.getLogger(__name__)

_PROMPT_NAME = "scheduling-create-loop-extractor-v1"


class ExtractCreateLoopInput(BaseModel):
    """Template variables for scheduling-create-loop-extractor-v1 (v7+)."""

    coordinator: str
    current_email: str
    thread_history: str


def _build_initial_messages(prompt: Any, data: ExtractCreateLoopInput) -> list[dict[str, str]]:
    """Compile the LangFuse prompt with the typed input."""
    input_dict = data.model_dump()
    if isinstance(prompt, ChatPromptClient):
        compiled = prompt.compile(**input_dict)
        return [dict(m) for m in compiled]
    compiled_str = prompt.compile(**input_dict)
    return [{"role": "system", "content": compiled_str}]


async def _call_llm(
    llm: LLMService,
    langfuse: Langfuse,
    prompt: Any,
    messages: list[dict[str, str]],
) -> LLMResponse:
    """Direct dispatch to the LLM service.

    Model / temperature / max_tokens come from the prompt's LangFuse config
    so version-pinned settings win. Updates the current span's `input` to
    the live messages list — including any retry follow-up — so the trace
    reflects exactly what the model saw.
    """
    config: dict = prompt.config or {}
    model = config.get("model", DEFAULT_MODEL)
    temperature = config.get("temperature", 0.0)
    max_tokens = config.get("max_tokens", 4096)

    langfuse.update_current_span(input=messages)

    return await llm.complete(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )


async def _extract_with_messages(
    llm: LLMService,
    langfuse: Langfuse,
    prompt: Any,
    messages: list[dict[str, str]],
    *,
    allow_retry: bool,
    prior_responses: list[str] | None = None,
    prior_diagnostics: list[dict] | None = None,
) -> CreateLoopExtraction:
    """One LLM round-trip + parse. On parse failure, retry once with a
    conversation-history follow-up.

    Span output accumulates raw responses + per-call diagnostics across the
    initial call and any retry, mirroring the loop classifier so traces
    stay consistent across the two direct-LLM paths.
    """
    prior_responses = prior_responses or []
    prior_diagnostics = prior_diagnostics or []

    response = await _call_llm(llm, langfuse, prompt, messages)
    responses = [*prior_responses, response.content]
    diagnostics = [
        *prior_diagnostics,
        response.to_diagnostics(attempt=len(prior_responses)),
    ]

    try:
        result = parse_loop_envelope(response.content)
    except LoopEnvelopeParseError as exc:
        if allow_retry:
            logger.warning(
                "create-loop extractor parse failed (%s) — retrying with follow-up "
                "[finish_reason=%s, completion_tokens=%s, model=%s, latency_ms=%.0f]",
                exc,
                response.finish_reason,
                response.usage.get("completion_tokens"),
                response.model,
                response.latency_ms,
            )
            follow_up = build_error_followup(
                [str(exc)], envelope_hint="<loop>{...}</loop> JSON object"
            )
            next_messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": follow_up},
            ]
            return await _extract_with_messages(
                llm,
                langfuse,
                prompt,
                next_messages,
                allow_retry=False,
                prior_responses=responses,
                prior_diagnostics=diagnostics,
            )
        langfuse.update_current_span(
            output={
                "raw_responses": responses,
                "call_diagnostics": diagnostics,
                "parse_error": str(exc),
            }
        )
        raise

    langfuse.update_current_span(
        output={
            "extraction": result.model_dump(),
            "raw_responses": responses,
            "call_diagnostics": diagnostics,
        }
    )
    return result


async def extract_create_loop_fields(
    *,
    llm: LLMService,
    langfuse: Langfuse,
    data: ExtractCreateLoopInput,
) -> CreateLoopExtraction:
    """Extract create-loop fields from a thread via the v7 prompt.

    Drives `llm.complete()` directly (no `LLMEndpoint`) so the v7 reasoning
    preamble + `<loop>` envelope contract works and a parse failure can
    retry through conversation history. Raises `LoopEnvelopeParseError` if
    the model can't produce a parseable envelope after one retry — the
    caller's existing fallback turns that into deterministic-only prefill.
    """
    prompt = fetch_prompt(langfuse, _PROMPT_NAME)
    with langfuse.start_as_current_observation(name="create-loop-extractor"):
        langfuse.update_current_span(
            metadata={
                "prompt_name": _PROMPT_NAME,
                "prompt_version": prompt.version,
                "prompt_labels": prompt.labels,
                "context": data.model_dump(),
            }
        )
        messages = _build_initial_messages(prompt, data)
        return await _extract_with_messages(llm, langfuse, prompt, messages, allow_retry=True)
