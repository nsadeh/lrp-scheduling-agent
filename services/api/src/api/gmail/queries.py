"""Load Gmail SQL queries via aiosql."""

from pathlib import Path

import aiosql

from api.observability import TracedQueries

_SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "queries"

queries = TracedQueries(
    aiosql.from_path(_SQL_DIR / "gmail_push.sql", "apsycopg", mandatory_parameters=False)
)

token_queries = TracedQueries(
    aiosql.from_path(_SQL_DIR / "gmail_tokens.sql", "apsycopg", mandatory_parameters=False)
)
