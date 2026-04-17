"""aiosql span wrapper.

Captures query name, duration, and row count for every aiosql call without
shipping parameters or result rows to Sentry — parameters include
candidate/coordinator emails and subject lines that must not leak.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

import sentry_sdk


class TracedQueries:
    """Proxy around an aiosql ``Queries`` object.

    Each async attribute access returns a wrapper that opens a ``db.query``
    span named after the query and records the returned row count.
    Non-callable attributes pass through unchanged.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._wrapped: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._wrapped:
            return self._wrapped[name]

        attr = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        @functools.wraps(attr)
        async def traced(*args: Any, **kwargs: Any) -> Any:
            with sentry_sdk.start_span(op="db.query", name=name) as span:
                result = await attr(*args, **kwargs)
                if isinstance(result, list):
                    span.set_data("db.rows_returned", len(result))
                elif result is None:
                    span.set_data("db.rows_returned", 0)
                else:
                    span.set_data("db.rows_returned", 1)
                return result

        self._wrapped[name] = traced
        return traced
