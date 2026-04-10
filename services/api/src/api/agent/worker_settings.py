"""arq WorkerSettings for the background job runner.

Run with: arq api.agent.worker_settings.WorkerSettings
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings
from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

from api.agent.llm import AnthropicProvider, LLMRouter, OpenAIProvider
from api.agent.service import AgentService
from api.agent.tracing import init_tracing
from api.agent.workers import (
    cleanup_old_processed_messages,
    process_gmail_notification,
    process_relevant_message,
    renew_gmail_watches,
    sync_gmail_history,
)
from api.gmail.auth import TokenStore
from api.gmail.client import GmailClient
from api.scheduling.service import LoopService

load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env", override=True)

# Configure logging so worker output is visible
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


def _redis_settings() -> RedisSettings:
    """Build RedisSettings from REDIS_URL env var."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return RedisSettings.from_dsn(redis_url)


async def startup(ctx: dict) -> None:
    """Initialize shared resources for the worker process."""
    # Initialise Langfuse tracing before creating LLM clients so that
    # the AnthropicInstrumentor can patch the SDK before first use.
    init_tracing()

    database_url = os.environ.get("DATABASE_URL", "postgresql://dev:dev@localhost:5432/lrp_dev")
    pool = AsyncConnectionPool(conninfo=database_url)
    await pool.open()
    ctx["db"] = pool

    encryption_key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    if encryption_key:
        token_store = TokenStore(db_pool=pool, encryption_key=encryption_key)
        gmail = GmailClient(token_store)
        ctx["token_store"] = token_store
        ctx["gmail"] = gmail
        logger.info("Worker: GmailClient initialized")
    else:
        logger.warning("Worker: GMAIL_TOKEN_ENCRYPTION_KEY not set — Gmail features unavailable")
        ctx["token_store"] = None
        ctx["gmail"] = None

    gmail_client = ctx.get("gmail")
    ctx["scheduling"] = LoopService(db_pool=pool, gmail=gmail_client)
    ctx["agent_service"] = AgentService(db_pool=pool)

    # LLM routers for classification and drafting
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

    classifier_primary = (
        AnthropicProvider(model="claude-haiku-4-5-20251001", api_key=anthropic_key)
        if anthropic_key
        else None
    )
    classifier_fallback = (
        OpenAIProvider(model="gpt-4o-mini", api_key=openai_key) if openai_key else None
    )
    drafter_primary = (
        AnthropicProvider(model="claude-sonnet-4-20250514", api_key=anthropic_key)
        if anthropic_key
        else None
    )
    drafter_fallback = OpenAIProvider(model="gpt-4o", api_key=openai_key) if openai_key else None

    if classifier_primary or classifier_fallback:
        ctx["classifier"] = LLMRouter(
            primary=classifier_primary or classifier_fallback,
            fallback=classifier_fallback if classifier_primary else None,
        )
        ctx["drafter"] = LLMRouter(
            primary=drafter_primary or drafter_fallback,
            fallback=drafter_fallback if drafter_primary else None,
        )
        logger.info("Worker: LLM routers initialized")
    else:
        ctx["classifier"] = None
        ctx["drafter"] = None
        logger.warning("Worker: No LLM API keys set — agent engine unavailable")

    logger.info("Worker: LoopService and AgentService initialized")


async def shutdown(ctx: dict) -> None:
    """Clean up shared resources."""
    db = ctx.get("db")
    if db:
        await db.close()
        logger.info("Worker: DB pool closed")


class WorkerSettings:
    """arq worker configuration."""

    functions: ClassVar[list] = [process_gmail_notification, process_relevant_message]

    cron_jobs: ClassVar[list] = [
        cron(renew_gmail_watches, hour={0, 6, 12, 18}, minute=0),
        cron(
            sync_gmail_history,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        cron(cleanup_old_processed_messages, hour=3, minute=0),
    ]

    redis_settings = _redis_settings()

    on_startup = startup
    on_shutdown = shutdown
