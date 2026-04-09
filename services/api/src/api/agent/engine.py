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
from api.agent.prompts import build_classification_prompt, build_draft_prompt

logger = logging.getLogger(__name__)


async def run_agent(
    ctx: AgentContext,
    classifier: LLMRouter,
    drafter: LLMRouter,
) -> AgentResult:
    """Run the two-step scheduling agent: classify, then draft if needed."""

    # Step 1: Classification (cheap, fast model)
    system, user = build_classification_prompt(ctx)
    classify_response = await classifier.complete(
        system=system,
        user=user,
        max_tokens=1024,
        temperature=0.1,
    )
    classification = _parse_classification(classify_response.content)

    # Step 2: Draft generation (only if action requires it)
    draft = None
    if classification.suggested_action in ACTIONS_REQUIRING_DRAFT:
        system, user = build_draft_prompt(ctx, classification)
        draft_response = await drafter.complete(
            system=system,
            user=user,
            max_tokens=2048,
            temperature=0.3,
        )
        draft = _parse_draft(draft_response.content)

    return AgentResult(classification=classification, draft=draft)


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
