"""Observability primitives — shared Sentry init, request-ID middleware,
and aiosql span wrapping. Imported by both the FastAPI app and the Arq worker
so error/performance data converges on a single Sentry project.
"""

from api.observability.db_spans import TracedQueries
from api.observability.sentry import RequestIdMiddleware, init_sentry

__all__ = ["RequestIdMiddleware", "TracedQueries", "init_sentry"]
