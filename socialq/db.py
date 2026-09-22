"""Postgres access.

Postgres is the queue (§5), so there is no ORM and no second datastore. Every
caller gets a plain psycopg connection with dict rows.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import database_url


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a connection, committing on clean exit and rolling back on error.

    `prepare_threshold=None` disables psycopg's automatic prepared statements.
    They are worth nothing at this volume and they break behind a connection
    pooler, which is what a hosted Postgres puts in front of you -- a failure
    that appears only under load, and only in production.
    """
    conn = psycopg.connect(
        url or database_url(), row_factory=dict_row, prepare_threshold=None
    )
    try:
        with conn:
            yield conn
    finally:
        conn.close()
