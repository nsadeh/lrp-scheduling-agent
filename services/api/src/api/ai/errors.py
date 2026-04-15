"""AI infrastructure exceptions.

Hierarchy mirrors gmail/exceptions.py — a base class with specific subtypes
for each failure mode. Callers catch AIError for blanket handling or specific
subtypes when recovery strategies differ.
"""


class AIError(Exception):
    """Base class for all AI infrastructure errors."""


class LangFuseUnavailableError(AIError):
    """LangFuse is unreachable and no cached prompt exists."""


class PromptNotFoundError(AIError):
    """The requested prompt name does not exist in LangFuse."""


class LLMUnavailableError(AIError):
    """All LLM providers failed within the latency budget."""


class LLMParseError(AIError):
    """LLM response could not be parsed into the expected output type."""

    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class LLMBudgetExceededError(AIError):
    """Token budget or spend limit exceeded."""
