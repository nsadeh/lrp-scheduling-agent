"""Typed getters for ``app.state`` attributes.

Rather than scattering ``getattr(request.app.state, "gmail", None)`` calls
across route handlers, every consumer goes through a helper here. Each
getter is typed for IDE / pyright use and consistent about None semantics:

- **Required state** (always populated during lifespan): direct accessor, no
  Optional return. Raises ``AttributeError`` if not set — a bug.
- **Optional state** (may be skipped when deps are missing, e.g. Redis when
  disabled, GmailClient when the encryption key is absent): returns ``None``.

The split matches what ``api.main.lifespan`` actually does — see there for
which services are always wired up vs. conditionally initialized.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from fastapi import Request
    from langfuse import Langfuse
    from psycopg_pool import AsyncConnectionPool

    from api.ai.llm_service import LLMService
    from api.drafts.service import DraftService
    from api.gmail.client import GmailClient
    from api.overview.service import OverviewService
    from api.scheduling.service import LoopService


# -- Required: set by lifespan, always available in production ------------


def get_db(request: Request) -> AsyncConnectionPool:
    return request.app.state.db


def get_scheduling(request: Request) -> LoopService:
    return request.app.state.scheduling


# -- Optional: may be None when the dep is disabled OR the test harness ---
# doesn't wire it up. Production lifespan sets all of these — callers should
# handle None as "feature unavailable, degrade gracefully".


def get_gmail(request: Request) -> GmailClient | None:
    """GmailClient, or None if ``GMAIL_TOKEN_ENCRYPTION_KEY`` is unset."""
    return getattr(request.app.state, "gmail", None)


def get_redis(request: Request) -> ArqRedis | None:
    """Arq Redis pool, or None if Redis is unreachable at startup."""
    return getattr(request.app.state, "redis", None)


def get_llm(request: Request) -> LLMService | None:
    return getattr(request.app.state, "llm_service", None)


def get_langfuse(request: Request) -> Langfuse | None:
    return getattr(request.app.state, "langfuse", None)


def get_draft_service(request: Request) -> DraftService | None:
    return getattr(request.app.state, "draft_service", None)


def get_overview_service(request: Request) -> OverviewService | None:
    """OverviewService if one has been set. Route code that needs a
    lazily-instantiated service should use ``addon.routes._get_overview_service``
    which constructs one from the db pool on first access.
    """
    return getattr(request.app.state, "overview_service", None)
