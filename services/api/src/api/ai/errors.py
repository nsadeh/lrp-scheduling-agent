"""AI infrastructure exceptions."""


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


class LLMBudgetExceededError(AIError):
    """Token budget or spend limit exceeded."""
