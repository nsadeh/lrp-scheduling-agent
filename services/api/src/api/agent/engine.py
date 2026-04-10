"""Agent execution engine: classify emails and generate drafts."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from api.agent.models import (
    ACTIONS_REQUIRING_DRAFT,
    AgentContext,
    AgentResult,
    ClassificationResult,
    DraftEmail,
)

if TYPE_CHECKING:
    from api.agent.llm import LLMRouter
import contextlib

from api.agent.prompts import build_classification_prompt, build_draft_prompt

logger = logging.getLogger(__name__)


def _get_langfuse():
    """Return the Langfuse singleton, or None if unavailable."""
    try:
        from langfuse import get_client

        return get_client()
    except Exception:
        return None


async def run_agent(
    ctx: AgentContext,
    classifier: LLMRouter,
    drafter: LLMRouter,
) -> AgentResult:
    """Run the two-step scheduling agent: classify, then draft if needed.

    Creates Langfuse spans for each step so that token usage, latency,
    and classification results are observable.
    """
    langfuse = _get_langfuse()

    # Build a concise trace input (avoid logging full email bodies)
    trace_input = {
        "from": ctx.new_message.from_.email if ctx.new_message.from_ else "unknown",
        "subject": ctx.new_message.subject,
        "has_loop": ctx.loop is not None,
        "loop_id": ctx.loop.id if ctx.loop else None,
    }

    # --- Parent span for the full agent pipeline ---
    pipeline_span = None
    if langfuse:
        try:
            pipeline_span = langfuse.trace(
                name="agent-pipeline",
                input=trace_input,
            )
        except Exception:
            logger.debug("Failed to create Langfuse trace", exc_info=True)

    try:
        # Step 1: Classification (cheap, fast model)
        classify_obs = None
        if pipeline_span:
            with contextlib.suppress(Exception):
                classify_obs = pipeline_span.span(name="classify-email")

        system, user = build_classification_prompt(ctx)
        classify_response = await classifier.complete(
            system=system,
            user=user,
            max_tokens=1024,
            temperature=0.1,
        )
        classification = _parse_classification(classify_response.content)

        if classify_obs:
            with contextlib.suppress(Exception):
                classify_obs.end(
                    output={
                        "classification": classification.classification.value,
                        "action": classification.suggested_action.value,
                        "confidence": classification.confidence,
                        "model": classify_response.model,
                        "input_tokens": classify_response.input_tokens,
                        "output_tokens": classify_response.output_tokens,
                        "latency_ms": classify_response.latency_ms,
                    },
                )

        # Step 2: Draft generation (only if action requires it)
        draft = None
        if classification.suggested_action in ACTIONS_REQUIRING_DRAFT:
            draft_obs = None
            if pipeline_span:
                with contextlib.suppress(Exception):
                    draft_obs = pipeline_span.span(name="draft-email")

            system, user = build_draft_prompt(ctx, classification)
            draft_response = await drafter.complete(
                system=system,
                user=user,
                max_tokens=2048,
                temperature=0.3,
            )
            draft = _parse_draft(draft_response.content)

            if draft_obs:
                with contextlib.suppress(Exception):
                    draft_obs.end(
                        output={
                            "has_draft": True,
                            "model": draft_response.model,
                            "input_tokens": draft_response.input_tokens,
                            "output_tokens": draft_response.output_tokens,
                            "latency_ms": draft_response.latency_ms,
                        },
                    )

        result = AgentResult(classification=classification, draft=draft)

        # Update the pipeline trace with output
        if pipeline_span:
            with contextlib.suppress(Exception):
                pipeline_span.update(
                    output={
                        "classification": classification.classification.value,
                        "action": classification.suggested_action.value,
                        "confidence": classification.confidence,
                        "has_draft": draft is not None,
                    },
                )

        return result

    except Exception as exc:
        if pipeline_span:
            with contextlib.suppress(Exception):
                pipeline_span.update(
                    output={"error": str(exc)},
                    level="ERROR",
                    status_message=str(exc),
                )
        raise


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_json(raw: str) -> str:
    """Extract JSON from raw LLM output, handling markdown code blocks."""
    # Try to find JSON inside a markdown code block first
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        return match.group(1).strip()
    # Otherwise assume the entire string is JSON
    return raw.strip()


def _parse_classification(raw: str) -> ClassificationResult:
    """Parse LLM output into ClassificationResult."""
    json_str = _extract_json(raw)
    data = json.loads(json_str)
    return ClassificationResult(**data)


def _parse_draft(raw: str) -> DraftEmail:
    """Parse LLM output into DraftEmail."""
    json_str = _extract_json(raw)
    data = json.loads(json_str)
    # Normalise null in_reply_to
    if data.get("in_reply_to") == "null":
        data["in_reply_to"] = None
    return DraftEmail(**data)
