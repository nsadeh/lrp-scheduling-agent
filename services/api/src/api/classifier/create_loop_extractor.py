"""Typed LLM endpoint for on-demand create-loop field extraction.

Runs against a formatted Gmail thread when the coordinator clicks
"Create New Loop" on a thread the classifier did not flag as
CREATE_LOOP. Emits the same CreateLoopExtraction shape the classifier
writes into action_data, so downstream form-prefill code handles both
paths uniformly. See rfcs/rfc-infer-create-loop-fields.md.
"""

from pydantic import BaseModel

from api.ai import llm_endpoint
from api.classifier.models import CreateLoopExtraction


class ExtractCreateLoopInput(BaseModel):
    """Template variables for scheduling-create-loop-extractor-v1."""

    thread_history: str
    coordinator_email: str


extract_create_loop_fields = llm_endpoint(
    name="extract_create_loop_fields",
    prompt_name="scheduling-create-loop-extractor-v1",
    input_type=ExtractCreateLoopInput,
    output_type=CreateLoopExtraction,
)
