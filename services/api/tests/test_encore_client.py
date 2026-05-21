"""Tests for the Encore pymssql client lifecycle and kill switch."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pymssql
import pytest

from api.encore import client as encore_client


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Clear singleton + env so each test starts clean."""
    encore_client._conn = None
    for var in (
        "ENCORE_MSSQL_HOST",
        "ENCORE_MSSQL_PORT",
        "ENCORE_MSSQL_DATABASE",
        "ENCORE_MSSQL_USER",
        "ENCORE_MSSQL_PASSWORD",
        "ENCORE_MSSQL_QUERY_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    encore_client._conn = None


class TestQueryTimeout:
    def test_default_is_five_seconds(self):
        assert encore_client.query_timeout_seconds() == 5

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("ENCORE_MSSQL_QUERY_TIMEOUT_SECONDS", "12")
        assert encore_client.query_timeout_seconds() == 12

    def test_falls_back_on_garbage(self, monkeypatch):
        monkeypatch.setenv("ENCORE_MSSQL_QUERY_TIMEOUT_SECONDS", "not-a-number")
        assert encore_client.query_timeout_seconds() == 5


class TestOpenConnection:
    def test_raises_when_credentials_missing(self):
        with pytest.raises(RuntimeError, match="ENCORE_MSSQL"):
            encore_client._open_connection()

    def test_passes_env_through_to_pymssql(self, monkeypatch):
        monkeypatch.setenv("ENCORE_MSSQL_HOST", "sql.example.com")
        monkeypatch.setenv("ENCORE_MSSQL_PORT", "1234")
        monkeypatch.setenv("ENCORE_MSSQL_DATABASE", "encore_prod")
        monkeypatch.setenv("ENCORE_MSSQL_USER", "lrp_ro")
        monkeypatch.setenv("ENCORE_MSSQL_PASSWORD", "secret")

        with patch.object(pymssql, "connect", return_value=MagicMock()) as mock_connect:
            encore_client._open_connection()
            mock_connect.assert_called_once()
            kwargs = mock_connect.call_args.kwargs
            assert kwargs["server"] == "sql.example.com"
            assert kwargs["port"] == 1234
            assert kwargs["user"] == "lrp_ro"
            assert kwargs["password"] == "secret"
            assert kwargs["database"] == "encore_prod"
            assert kwargs["timeout"] == 5
            assert kwargs["as_dict"] is True


class TestWithConnection:
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("ENCORE_MSSQL_HOST", "h")
        monkeypatch.setenv("ENCORE_MSSQL_DATABASE", "d")
        monkeypatch.setenv("ENCORE_MSSQL_USER", "u")
        monkeypatch.setenv("ENCORE_MSSQL_PASSWORD", "p")

    def test_opens_lazily_and_caches(self, monkeypatch):
        self._set_env(monkeypatch)
        fake_conn = MagicMock()
        with patch.object(pymssql, "connect", return_value=fake_conn) as mock_connect:
            encore_client.with_connection(lambda conn: "first")
            encore_client.with_connection(lambda conn: "second")
            assert mock_connect.call_count == 1
            assert encore_client._conn is fake_conn

    def test_passes_connection_to_callback(self, monkeypatch):
        self._set_env(monkeypatch)
        fake_conn = MagicMock()
        with patch.object(pymssql, "connect", return_value=fake_conn):
            result = encore_client.with_connection(lambda conn: ("ok", conn))
            assert result == ("ok", fake_conn)

    def test_resets_connection_on_operational_error(self, monkeypatch):
        self._set_env(monkeypatch)
        fake_conn = MagicMock()

        def boom(_conn):
            raise pymssql.OperationalError("network down")

        with patch.object(pymssql, "connect", return_value=fake_conn):
            with pytest.raises(pymssql.OperationalError):
                encore_client.with_connection(boom)
            assert encore_client._conn is None
            fake_conn.close.assert_called_once()

    def test_lock_serializes_callers(self, monkeypatch):
        """Two threads cannot hold the connection at the same time."""
        self._set_env(monkeypatch)
        fake_conn = MagicMock()

        observed_concurrency = 0
        running = 0
        running_lock = threading.Lock()

        def task(_conn):
            nonlocal observed_concurrency, running
            with running_lock:
                running += 1
                observed_concurrency = max(observed_concurrency, running)
            # Hold the connection briefly so contention is detectable.
            import time

            time.sleep(0.02)
            with running_lock:
                running -= 1

        with patch.object(pymssql, "connect", return_value=fake_conn):
            threads = [
                threading.Thread(target=encore_client.with_connection, args=(task,))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert observed_concurrency == 1


class TestModuleImport:
    def test_module_imports_with_no_env(self):
        """Importing the module must not touch pymssql or env."""
        # Already imported at the top; re-import to make the assertion explicit.
        from importlib import reload

        from api.encore import client as fresh

        reload(fresh)
        assert fresh._conn is None
