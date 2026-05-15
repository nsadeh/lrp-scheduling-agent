"""Shared helpers for direct-LLM classifier agents and the loop extractor.

`NextActionAgent`, `LoopClassifier`, and the manual `extract_create_loop_fields`
extractor all drive `llm.complete()` directly (bypassing `LLMEndpoint`) so they
can use conversation-history retries and parse the reasoning-preamble +
envelope contract their prompts emit. The bits that don't differ live here.

Note: the envelope parsers are intentionally separate from
`LLMEndpoint._try_parse`. The endpoint parses a bare JSON object against a
caller-supplied Pydantic schema (still used by other typed endpoints). The
envelope parsers handle the reasoning-preamble + tag-wrapped contract the
v26 loop-classifier (`<suggestions>[…]</suggestions>` JSON array), #80
next-action-agent (same), and v7 create-loop extractor (`<loop>{…}</loop>`
single JSON object) prompts emit, hard-coded to their respective domain
types. Unifying with the endpoint would mean either breaking the bare-JSON
contract or making the endpoint classifier-domain-aware — neither is worth it.

Per-LLM-call diagnostics (finish_reason, latency, token counts, etc.) live
on `LLMResponse.to_diagnostics` rather than here — it's a property of the
response, not of the consumer.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import ValidationError

from api.classifier.models import (
    ClassificationResult,
    CreateLoopExtraction,
    SuggestionItem,
)

logger = logging.getLogger(__name__)

# `<suggestions>…</suggestions>` envelope around the JSON array.
SUGGESTIONS_RE = re.compile(r"<suggestions>(.*?)</suggestions>", re.DOTALL)
# Fallback: first bare JSON array we can find.
JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")

# `<loop>…</loop>` envelope around a single JSON object.
LOOP_RE = re.compile(r"<loop>(.*?)</loop>", re.DOTALL)
# Fallback: first bare JSON object we can find.
JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


class SuggestionsParseError(Exception):
    """Raised when an LLM response can't be parsed into a SuggestionItem list."""


class LoopEnvelopeParseError(Exception):
    """Raised when an LLM response can't be parsed into a CreateLoopExtraction."""


def parse_suggestions_envelope(content: str) -> ClassificationResult:
    """Extract the `<suggestions>` JSON array from the LLM response.

    Tolerates leading reasoning text, markdown fences, and a fallback path
    where the envelope tags are missing but a JSON array is present.
    """
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()

    match = SUGGESTIONS_RE.search(text)
    if match:
        inner = match.group(1).strip()
    else:
        array_match = JSON_ARRAY_RE.search(text)
        if not array_match:
            raise SuggestionsParseError(
                "response did not contain a <suggestions>…</suggestions> envelope or a JSON array"
            )
        inner = array_match.group(0)

    try:
        data = json.loads(inner)
    except json.JSONDecodeError as exc:
        raise SuggestionsParseError(f"suggestions JSON failed to parse: {exc}") from exc

    if not isinstance(data, list):
        raise SuggestionsParseError("suggestions payload must be a JSON array")

    suggestions: list[SuggestionItem] = []
    for raw in data:
        try:
            suggestions.append(SuggestionItem.model_validate(raw))
        except ValidationError as exc:
            raise SuggestionsParseError(f"suggestion item failed schema validation: {exc}") from exc

    return ClassificationResult(suggestions=suggestions)


def parse_loop_envelope(content: str) -> CreateLoopExtraction:
    """Extract the `<loop>` single JSON object from the LLM response.

    The v7 create-loop extractor prompt emits a numbered precognition
    checklist followed by `<loop>{ …one JSON object… }</loop>`. Tolerates
    leading reasoning text, markdown fences, and a fallback path where the
    envelope tags are missing but a JSON object is present.
    """
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()

    match = LOOP_RE.search(text)
    if match:
        inner = match.group(1).strip()
    else:
        object_match = JSON_OBJECT_RE.search(text)
        if not object_match:
            raise LoopEnvelopeParseError(
                "response did not contain a <loop>…</loop> envelope or a JSON object"
            )
        inner = object_match.group(0)

    try:
        data = json.loads(inner)
    except json.JSONDecodeError as exc:
        raise LoopEnvelopeParseError(f"loop JSON failed to parse: {exc}") from exc

    if not isinstance(data, dict):
        raise LoopEnvelopeParseError("loop payload must be a JSON object")

    try:
        return CreateLoopExtraction.model_validate(data)
    except ValidationError as exc:
        raise LoopEnvelopeParseError(f"loop payload failed schema validation: {exc}") from exc


def build_error_followup(
    errors: list[str],
    *,
    envelope_hint: str = "<suggestions>[...]</suggestions> array",
) -> str:
    """Build the user-role follow-up message for the conversation-history retry.

    `envelope_hint` names the output shape the model should re-emit — defaults
    to the classifier's suggestions array; the loop extractor passes
    ``"<loop>{...}</loop>"``.
    """
    bullets = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous response resulted in the following errors:\n"
        f"{bullets}\n\n"
        f"Please produce a corrected {envelope_hint}."
    )
