"""Langfuse tracing initialization for the scheduling agent.

Call ``init_tracing()`` **once** at application startup, after environment
variables have been loaded (``load_dotenv``) and **before** creating any
Anthropic or OpenAI clients.

The function is idempotent — calling it more than once is safe.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_initialized = False


def init_tracing() -> None:
    """Set up Langfuse tracing and auto-instrument the Anthropic SDK.

    Does nothing if Langfuse credentials are not configured or if
    tracing has already been initialised in this process.
    """
    global _initialized
    if _initialized:
        return

    try:
        import os

        # Skip if no Langfuse credentials
        if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
            logger.info("Tracing: Langfuse credentials not set — tracing disabled")
            _initialized = True
            return

        # Initialise the Langfuse singleton (reads LANGFUSE_* env vars)
        from langfuse import get_client

        langfuse = get_client()
        if not langfuse.auth_check():
            logger.warning("Tracing: Langfuse auth check failed — tracing disabled")
            _initialized = True
            return

        # Auto-instrument the Anthropic SDK via OpenTelemetry.
        # This patches anthropic.Client / AsyncAnthropic so that every
        # messages.create() call emits an OTEL span captured by Langfuse.
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

        AnthropicInstrumentor().instrument()

        logger.info("Tracing: Langfuse + AnthropicInstrumentor initialised")
        _initialized = True

    except Exception:
        logger.warning("Tracing: failed to initialise — continuing without tracing", exc_info=True)
        _initialized = True
