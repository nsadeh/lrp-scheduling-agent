"""Typed LLM endpoint for email classification.

Uses the llm_endpoint factory from the AI infrastructure to define
a single async callable: classify_email(). The endpoint fetches the
scheduling-classifier-v3 chat prompt from LangFuse, fills template
variables with pre-formatted context, calls the LLM, and parses the
response into a ClassificationResult.
"""

from pydantic import BaseModel

from api.ai import llm_endpoint
from api.classifier.models import ClassificationResult


class ClassifyEmailInput(BaseModel):
    """Input for the classify_email endpoint — template variables for the prompt.

    System-level variables (stage_states, transitions) provide the state
    machine vocabulary. User-level variables provide the email context.
    """

    # System-level: state machine definitions
    stage_states: str
    transitions: str

    # User-level: email context
    email: str
    thread_history: str
    loop_state: str
    active_loops_summary: str
    events: str
    direction: str


classify_email = llm_endpoint(
    name="classify_email",
    prompt_name="scheduling-classifier-v3",
    input_type=ClassifyEmailInput,
    output_type=ClassificationResult,
)
