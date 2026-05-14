"""Typed LLM endpoints for the two-stage classification pipeline.

determine_next_action — Next Action Agent (linked threads)

The loop classifier no longer uses `llm_endpoint`. It drives `llm.complete()`
directly via `LoopClassifier` (see `loop_classifier.py`) so it can use
conversation-history retries and parse the `<suggestions>` envelope the
v26 prompt emits, the same way `NextActionAgent` already does.

Action constraints are enforced by per-class guardrails, not by schema.
"""

from api.ai import llm_endpoint
from api.classifier.models import ClassificationResult
from api.classifier.schemas import NextActionInput

determine_next_action = llm_endpoint(
    name="determine_next_action",
    prompt_name="next-action-agent",
    input_type=NextActionInput,
    output_type=ClassificationResult,
)
