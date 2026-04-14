"""AI infrastructure — LangFuse, multi-provider LLM, and typed endpoints.

Public API:
    init_langfuse()   — create a LangFuse client (or None if keys not set)
    init_llm_service() — create an LLMService (or None if no provider keys)
    llm_endpoint()    — define a typed LLM endpoint
    observe           — LangFuse @observe() decorator for tracing
    LangfuseFlushMiddleware — FastAPI middleware to flush traces per request
"""

from api.ai.endpoint import llm_endpoint
from api.ai.errors import (
    AIError,
    LangFuseUnavailableError,
    LLMBudgetExceededError,
    LLMParseError,
    LLMUnavailableError,
    PromptNotFoundError,
)
from api.ai.langfuse_client import LangfuseFlushMiddleware, init_langfuse, observe
from api.ai.llm_service import LLMResponse, LLMService, init_llm_service

__all__ = [
    "AIError",
    "LLMBudgetExceededError",
    "LLMParseError",
    "LLMResponse",
    "LLMService",
    "LLMUnavailableError",
    "LangFuseUnavailableError",
    "LangfuseFlushMiddleware",
    "PromptNotFoundError",
    "init_langfuse",
    "init_llm_service",
    "llm_endpoint",
    "observe",
]
