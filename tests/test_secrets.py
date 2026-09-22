"""The secret store. §8.2.1, §8.3.

The store is what makes token refresh possible: §14 calls Instagram's 60-day
expiry the most likely production failure, and a refreshed token that cannot be
saved is the same thing as no refresh at all.
"""

from __future__ import annotations

import pytest

from socialq.secrets import (
    EnvSecretStore,
    MissingSecret,
    PostgresSecretStore,
    generate_key,
)

KEY = generate_key()


@pytest.fixture
def store(conn, database_url):
    return PostgresSecretStore(database_url, key=KEY)


def test_a_secret_round_trips(store):
    store.set("IG_TOKEN", "a-long-lived-token")
    assert store.get("IG_TOKEN") == "a-long-lived-token"


def test_the_value_is_not_stored_in_plaintext(store, conn):
    """A leaked database dump must not be a leaked Instagram account."""
    store.set("IG_TOKEN", "plaintext-would-be-here")

    raw = conn.execute(
        "SELECT value FROM secrets WHERE ref = 'IG_TOKEN'"
    ).fetchone()["value"]

    assert b"plaintext-would-be-here" not in bytes(raw)


def test_setting_again_replaces_rather_than_duplicates(store, conn):
    """Token refresh overwrites; two rows would mean reading a stale token."""
    store.set("IG_TOKEN", "old")
    store.set("IG_TOKEN", "new")

    assert store.get("IG_TOKEN") == "new"
    assert conn.execute(
        "SELECT count(*) AS n FROM secrets WHERE ref = 'IG_TOKEN'"
    ).fetchone()["n"] == 1


def test_a_missing_secret_raises_rather_than_returning_empty(store):
    with pytest.raises(MissingSecret):
        store.get("NEVER_SET_ANYWHERE")


def test_an_unset_secret_is_seeded_from_the_environment_once(store, monkeypatch):
    """First deploy reads from the runtime's own secrets; every later read
    comes from the store, which rotation can write to."""
    monkeypatch.setenv("SEEDED_TOKEN", "from-the-environment")

    assert store.get("SEEDED_TOKEN") == "from-the-environment"

    # Now it is in the store, so the environment no longer matters.
    monkeypatch.delenv("SEEDED_TOKEN")
    assert store.get("SEEDED_TOKEN") == "from-the-environment"


def test_the_wrong_key_cannot_read_the_secret(store, database_url):
    """The key lives outside the database on purpose."""
    from cryptography.fernet import InvalidToken

    store.set("IG_TOKEN", "secret")
    other = PostgresSecretStore(database_url, key=generate_key())

    with pytest.raises(InvalidToken):
        other.get("IG_TOKEN")


def test_a_missing_key_is_an_explicit_error_not_a_crash(database_url, monkeypatch):
    monkeypatch.delenv("SOCIALQ_SECRET_KEY", raising=False)
    monkeypatch.setattr("socialq.secrets.load_dotenv", lambda *a, **k: None)

    with pytest.raises(MissingSecret, match="SOCIALQ_SECRET_KEY"):
        PostgresSecretStore(database_url).set("ANYTHING", "value")


def test_touch_is_a_no_op_because_rows_do_not_expire(store):
    """Modal's Dict entries expire after 7 days; these do not. The method
    exists so callers need not know which store they were handed."""
    assert store.touch(["a", "b"]) == 2


def test_refs_lists_what_is_stored(store):
    store.set("ONE", "1")
    store.set("TWO", "2")
    assert store.refs() == ["ONE", "TWO"]


def test_the_env_store_still_refuses_to_write(monkeypatch):
    """Kept as the local fallback: better to refuse than to lose a token."""
    monkeypatch.setenv("IG_TOKEN", "value")
    with pytest.raises(NotImplementedError, match="read-only"):
        EnvSecretStore().set("IG_TOKEN", "new")
