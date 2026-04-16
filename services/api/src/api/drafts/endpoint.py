"""Typed LLM endpoint for email draft generation."""

from api.ai.endpoint import llm_endpoint
from api.drafts.models import DraftOutput, GenerateDraftInput

generate_draft_content = llm_endpoint(
    name="generate_draft",
    prompt_name="draft-email-v1",
    input_type=GenerateDraftInput,
    output_type=DraftOutput,
)
