"""LangFuse initialization and prompt fetching.

Provides prompt management with caching and LLM call tracing via
the @observe() decorator. Prompts are fetched by name and label
(default: "production"). The SDK caches after first fetch so
LangFuse being temporarily unreachable does not break the service.
"""

from __future__ import annotations

import logging
import os

from langfuse import Langfuse

from api.ai.errors import LangFuseUnavailableError, PromptNotFoundError

logger = logging.getLogger(__name__)


def init_langfuse() -> Langfuse | None:
    """Initialize the LangFuse client from environment variables.

    Returns None if keys are not configured (graceful degradation).
    """
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        logger.warning("LANGFUSE keys not set — AI observability disabled")
        return None

    host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get(
        "LANGFUSE_HOST", "https://cloud.langfuse.com"
    )

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
    )
    logger.info("LangFuse client initialized (host=%s)", host)
    return client


def fetch_prompt(
    langfuse: Langfuse,
    name: str,
    *,
    label: str = "production",
) -> object:
    """Fetch a prompt from LangFuse by name and label.

    Returns the LangFuse prompt object with .prompt, .config, and .compile() methods.

    Raises:
        LangFuseUnavailableError: If LangFuse is unreachable and no cache exists.
        PromptNotFoundError: If the prompt name doesn't exist.
    """
    try:
        prompt = langfuse.get_prompt(name, label=label)
    except Exception as exc:
        error_msg = str(exc).lower()
        if "not found" in error_msg or "404" in error_msg:
            raise PromptNotFoundError(f"Prompt '{name}' not found in LangFuse") from exc
        raise LangFuseUnavailableError(
            f"Failed to fetch prompt '{name}' from LangFuse: {exc}"
        ) from exc

    if prompt.is_fallback:
        logger.warning("Serving cached prompt for '%s' (LangFuse unreachable)", name)

    return prompt
