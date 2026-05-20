"""Load Encore MS SQL queries via aiosql with the pymssql driver."""

from pathlib import Path

import aiosql

_SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "queries"

queries = aiosql.from_path(_SQL_DIR / "encore.sql", "pymssql", mandatory_parameters=False)
