"""pymssql connection lifecycle for the Encore resolver.

Single shared connection per process, opened lazily on first use. pymssql
connections are not coroutine-safe, so callers wrap `with_cursor` in
`asyncio.to_thread` and a `threading.Lock` serializes cursor access. Reset
on OperationalError so transient network blips don't kill the worker.

Kill switch: `ENCORE_RESOLVER_ENABLED=false` makes `is_enabled()` return
False; the resolver short-circuits without ever touching pymssql.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

import pymssql

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 1433
_DEFAULT_TIMEOUT = 5

_lock = threading.Lock()
_conn: pymssql.Connection | None = None


def is_enabled() -> bool:
    """True unless the kill switch is set to a falsy value."""
    return os.environ.get("ENCORE_RESOLVER_ENABLED", "true").lower() not in {"false", "0", "no"}


def query_timeout_seconds() -> int:
    raw = os.environ.get("ENCORE_MSSQL_QUERY_TIMEOUT_SECONDS")
    try:
        return int(raw) if raw else _DEFAULT_TIMEOUT
    except ValueError:
        return _DEFAULT_TIMEOUT


def _open_connection() -> pymssql.Connection:
    host = os.environ.get("ENCORE_MSSQL_HOST")
    database = os.environ.get("ENCORE_MSSQL_DATABASE")
    user = os.environ.get("ENCORE_MSSQL_USER")
    password = os.environ.get("ENCORE_MSSQL_PASSWORD")
    if not (host and database and user and password):
        raise RuntimeError(
            "Encore MSSQL credentials missing: set ENCORE_MSSQL_HOST, "
            "ENCORE_MSSQL_DATABASE, ENCORE_MSSQL_USER, ENCORE_MSSQL_PASSWORD."
        )

    port_raw = os.environ.get("ENCORE_MSSQL_PORT")
    port = int(port_raw) if port_raw else _DEFAULT_PORT

    return pymssql.connect(
        server=host,
        port=port,
        user=user,
        password=password,
        database=database,
        timeout=query_timeout_seconds(),
        login_timeout=query_timeout_seconds(),
        as_dict=True,
    )


def _reset_connection() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            logger.exception("error closing stale pymssql connection")
    _conn = None


def with_cursor[T](fn: Callable[[Any], T]) -> T:
    """Run a sync callable with a pymssql cursor under the module lock.

    Wrap this in `asyncio.to_thread` from the caller. On OperationalError
    we reset the connection so the next call reconnects cleanly.
    """
    global _conn
    with _lock:
        if _conn is None:
            _conn = _open_connection()
        try:
            with _conn.cursor() as cur:
                return fn(cur)
        except pymssql.OperationalError:
            _reset_connection()
            raise
