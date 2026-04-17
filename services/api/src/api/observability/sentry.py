"""Shared Sentry initialization and request-ID middleware.

Both the FastAPI app and the Arq worker call ``init_sentry`` on startup with
their ``service`` tag. A single Sentry project collects events from both; the
``service`` tag distinguishes API vs worker without splitting projects (keeps
cross-component request traces intact).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-Id"


def init_sentry(*, service: str) -> None:
    """Initialize Sentry for a service.

    INFO logs are captured as breadcrumbs automatically (attached to any later
    error event) and also forwarded to Sentry Logs for retention beyond
    Railway's log-drain window — that preserves the "where did the good path
    stop" debugging trail without making INFO events burn the error quota.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        logging.getLogger(__name__).info("SENTRY_DSN not set — Sentry disabled for %s", service)
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("ENVIRONMENT", "development"),
        release=os.environ.get("RAILWAY_GIT_COMMIT_SHA") or None,
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.2")),
        send_default_pii=False,
        integrations=[
            FastApiIntegration(),
            AsyncioIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        _experiments={"enable_logs": True},
    )
    sentry_sdk.set_tag("service", service)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Honor incoming X-Request-Id or mint one, expose as Sentry tag + header.

    The same id propagates into any Arq job enqueued during the request when
    callers forward ``request.state.request_id`` as a job arg.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        sentry_sdk.set_tag("request_id", request_id)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
