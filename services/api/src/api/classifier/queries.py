"""SQL query loader for suggestion queries."""

from pathlib import Path

import aiosql

_SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "queries"
queries = aiosql.from_path(_SQL_DIR / "suggestions.sql", "apsycopg", mandatory_parameters=False)
