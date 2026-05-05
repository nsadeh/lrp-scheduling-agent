"""Typed LLM endpoints for the two-stage classification pipeline.

classify_new_thread — Loop Classifier (unlinked threads)
determine_next_action — Next Action Agent (linked threads)

Both endpoints output the existing ClassificationResult schema.
Action constraints are enforced by guardrails in each hook class.
"""

from api.ai import llm_endpoint
from api.classifier.models import ClassificationResult
from api.classifier.schemas import LoopClassifierInput, NextActionInput

classify_new_thread = llm_endpoint(
    name="classify_new_thread",
    prompt_name="scheduling-new-loop-classifier",
    input_type=LoopClassifierInput,
    output_type=ClassificationResult,
)

determine_next_action = llm_endpoint(
    name="determine_next_action",
    prompt_name="next-action-agent",
    input_type=NextActionInput,
    output_type=ClassificationResult,
)
