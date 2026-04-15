"""LangFuse initialization, prompt fetching, and tracing middleware.

The Langfuse client is a singleton created at app startup. It provides:
- Prompt fetching with SDK-level caching (survives transient LangFuse outages)
- The @observe() decorator for hierarchical tracing of LLM calls
- A FastAPI middleware that flushes traces at the end of each request

Cold-start behavior: if LangFuse is unreachable on the first prompt fetch
(no cache), the SDK raises an exception. We fail loudly rather than serving
stale/divergent prompts — see RFC section "Cold-start behavior".
"""

import logging
import os

from langfuse import Langfuse, observe
from langfuse.model import ChatPromptClient, TextPromptClient

from api.ai.errors import LangFuseUnavailableError, PromptNotFoundError

logger = logging.getLogger(__name__)

# Re-export observe so callers import from this module
__all__ = ["fetch_prompt", "init_langfuse", "observe"]


def init_langfuse() -> Langfuse | None:
    """Create and return a LangFuse client, or None if keys are not configured.

    Required env vars: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY.
    Optional:
        LANGFUSE_HOST — defaults to LangFuse Cloud.
        LANGFUSE_ENVIRONMENT — tags all traces with this environment string
            (e.g., "development", "production"). Use this to separate dev
            traces from prod in the LangFuse dashboard.
    """
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        logger.warning("LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set — LangFuse disabled")
        return None

    host = os.environ.get("LANGFUSE_HOST")
    environment = os.environ.get("LANGFUSE_ENVIRONMENT")

    kwargs: dict = {
        "public_key": public_key,
        "secret_key": secret_key,
    }
    if host:
        kwargs["host"] = host
    if environment:
        kwargs["environment"] = environment

    client = Langfuse(**kwargs)
    logger.info(
        "LangFuse client initialized (host=%s, environment=%s)",
        host or "cloud",
        environment or "default",
    )
    return client


# Default prompt label — "production" in prod, overridable via env for dev
DEFAULT_PROMPT_LABEL = os.environ.get("LANGFUSE_PROMPT_LABEL", "production")


def fetch_prompt(
    client: Langfuse,
    name: str,
    *,
    label: str | None = None,
    prompt_type: str = "text",
) -> TextPromptClient | ChatPromptClient:
    """Fetch a prompt from LangFuse by name.

    Uses the SDK's built-in caching — after the first successful fetch, the
    cached version is returned if LangFuse is unreachable. On a true cold
    start with LangFuse down, raises LangFuseUnavailableError.

    The prompt's .config dict holds model parameters (model, temperature,
    max_tokens) that the LLM service and endpoint factory use.

    Args:
        label: Prompt label to fetch. Defaults to LANGFUSE_PROMPT_LABEL env var
            (which defaults to "production"). Set to "development" in .env
            to iterate on prompts without affecting prod.
    """
    resolved_label = label or DEFAULT_PROMPT_LABEL
    try:
        prompt = client.get_prompt(name, label=resolved_label, type=prompt_type)
    except Exception as exc:
        error_msg = str(exc).lower()
        if "not found" in error_msg:
            raise PromptNotFoundError(f"Prompt '{name}' not found in LangFuse") from exc
        raise LangFuseUnavailableError(
            f"Failed to fetch prompt '{name}' from LangFuse: {exc}"
        ) from exc

    if prompt.is_fallback:
        logger.warning("Serving cached/fallback prompt for '%s' — LangFuse may be degraded", name)

    return prompt
