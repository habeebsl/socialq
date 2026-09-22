"""Test fixtures.

Tests run against a real Postgres — the claim query (§5) is `FOR UPDATE SKIP
LOCKED`, which no in-memory substitute reproduces. See CONTRIBUTING for how to
start one; the default URL matches that container.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from socialq.migrate import migrate

DEFAULT_URL = "postgresql://socialq:socialq@localhost:55432/socialq"


def _admin_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)


@pytest.fixture(scope="session")
def database_url() -> str:
    """A migrated, throwaway database for the whole session."""
    admin = _admin_url()
    name = "socialq_test"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    migrate(url)
    return url


@pytest.fixture
def conn(database_url: str):
    """A connection whose writes are rolled back after the test."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        yield connection
        connection.rollback()
        # Truncate rather than relying on rollback alone: some tests open their
        # own connections to exercise concurrent claims.
        with connection.cursor() as cur:
            cur.execute(
                "TRUNCATE attempts, targets, posts, media, secrets,"
                " publisher_credentials, accounts, projects CASCADE"
            )
        connection.commit()
