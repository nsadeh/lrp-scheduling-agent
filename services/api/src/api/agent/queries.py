"""Load agent SQL queries via aiosql."""

from pathlib import Path

import aiosql

_SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "queries"

queries = aiosql.from_path(_SQL_DIR / "agent.sql", "apsycopg", mandatory_parameters=False)
