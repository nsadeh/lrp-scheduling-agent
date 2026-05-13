"""Input schemas for the two LLM endpoints.

LoopClassifierInput feeds the scheduling-new-loop-classifier prompt.
NextActionInput feeds the next-action-agent prompt.

Both prompts output the existing ClassificationResult — action constraints
are enforced by guardrails in the respective hook classes, not by schema.
"""

from pydantic import BaseModel


class LoopClassifierInput(BaseModel):
    """Template variables for the scheduling-new-loop-classifier prompt.

    Only used for inbound emails on threads not yet linked to any loop.
    """

    coordinator: str
    date: str
    email: str
    thread_history: str
    active_loops_summary: str
    error: str


class NextActionInput(BaseModel):
    """Template variables for the next-action-agent prompt.

    Used for emails (inbound or outgoing) on threads already linked to a loop.
    All four fields are XML-formatted strings — the prompt parses them as
    structured input. Conversational retries (errors, coordinator answers)
    are handled via the LLM message history, not as input fields.
    """

    date: str
    thread_history: str
    email: str
    loops: str
