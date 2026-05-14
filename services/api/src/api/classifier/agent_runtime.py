"""Shared helpers for direct-LLM classifier agents.

`NextActionAgent` and `LoopClassifier` both drive `llm.complete()` directly
(bypassing `LLMEndpoint`) so they can use conversation-history retries and
parse the `<suggestions>` envelope the agent prompts emit. The bits that
don't differ between them live here.

Note: the envelope parser is intentionally separate from
`LLMEndpoint._try_parse`. The endpoint parses a bare JSON object against a
caller-supplied Pydantic schema and serves the extractor endpoints
(`extract_create_loop_fields` etc.). The envelope parser handles the
reasoning-preamble + `<suggestions>[…]</suggestions>` contract the v26
loop-classifier and #80 next-action-agent prompts emit, and is hard-coded
to `ClassificationResult` / `SuggestionItem`. Unifying them would mean
either breaking the extractor contract or making the endpoint
classifier-domain-aware — neither is worth it.

Per-LLM-call diagnostics (finish_reason, latency, token counts, etc.) live
on `LLMResponse.to_diagnostics` rather than here — it's a property of the
response, not of the classifier.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import ValidationError

from api.classifier.models import ClassificationResult, SuggestionItem

logger = logging.getLogger(__name__)

# `<suggestions>…</suggestions>` envelope around the JSON array.
SUGGESTIONS_RE = re.compile(r"<suggestions>(.*?)</suggestions>", re.DOTALL)
# Fallback: first bare JSON array we can find.
JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


class SuggestionsParseError(Exception):
    """Raised when an LLM response can't be parsed into a SuggestionItem list."""


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


def build_error_followup(errors: list[str]) -> str:
    """Build the user-role follow-up message for the conversation-history retry."""
    bullets = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous suggestions resulted in the following errors:\n"
        f"{bullets}\n\n"
        "Please produce a corrected <suggestions>[...]</suggestions> array."
    )
