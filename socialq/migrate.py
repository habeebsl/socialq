"""Migration runner.

Plain numbered .sql files applied in order and recorded in schema_migrations.
Alembic would buy autogeneration we do not want: the schema in SOCIALQ_PLAN.md
§4 is the specification, and migrations should read like it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from .config import database_url

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary but fixed: two workers booting at once must not both migrate.
_LOCK_KEY = 0x50C1A190

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def pending(conn: psycopg.Connection) -> list[Path]:
    """Migration files not yet recorded as applied, in order."""
    # An explicit tuple factory: callers may hand us a dict_row connection,
    # and row[0] then raises KeyError rather than returning the version.
    from psycopg.rows import tuple_row

    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(_BOOTSTRAP)
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
    conn.commit()
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.stem not in applied]


def migrate(url: str | None = None) -> list[str]:
    """Apply every pending migration. Returns the versions applied."""
    applied: list[str] = []
    with psycopg.connect(url or database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        conn.commit()
        try:
            for path in pending(conn):
                with conn.cursor() as cur:
                    cur.execute(path.read_text())
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)",
                        (path.stem,),
                    )
                conn.commit()
                applied.append(path.stem)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            conn.commit()
    return applied


def main() -> int:
    versions = migrate()
    if versions:
        for version in versions:
            print(f"applied {version}")
    else:
        print("already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
