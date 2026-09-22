"""Secret storage.

§8.2.1: `publisher_credentials.api_key_ref` is a key into a secret store, never
the secret itself. Tokens rotate (§8.3), so the store must be writable -- which
the environment is not.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from .config import load_dotenv


class SecretStore(Protocol):
    def get(self, ref: str) -> str: ...
    def set(self, ref: str, value: str) -> None: ...


class MissingSecret(KeyError):
    """No secret under that ref."""


class EnvSecretStore:
    """Reads from the environment. Cannot write.

    Fine for PostPeer keys, which never rotate on their own. Instagram tokens
    do rotate, so a worker configured this way will refuse the refresh rather
    than appear to succeed and lose the new token -- which would burn the
    account's 60 days silently.
    """

    def get(self, ref: str) -> str:
        value = os.environ.get(ref)
        if not value:
            load_dotenv()
            value = os.environ.get(ref)
        if not value:
            raise MissingSecret(ref)
        return value

    def set(self, ref: str, value: str) -> None:
        raise NotImplementedError(
            f"cannot write {ref}: the environment is read-only. Configure a "
            "writable secret store before enabling token refresh (§8.3)."
        )


def generate_key() -> str:
    """A fresh encryption key for PostgresSecretStore."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


class PostgresSecretStore:
    """Encrypted secrets in the database. The default in production.

    §8.2.1 says api_key_ref must point at a secret store rather than hold the
    secret, and §8.3 needs that store writable so a refreshed token survives.
    Postgres is already the system of record and every job already connects to
    it, so this adds no new dependency and works identically on Appwrite,
    Modal, Railway or cron -- the point being not to re-couple the runtime.

    The value is encrypted with a key kept OUTSIDE the database
    (SOCIALQ_SECRET_KEY, from the runtime's own secret manager). A leaked
    database dump is then not a leaked Instagram account, which matters because
    the queue's contents are far less sensitive than its credentials and the
    two would otherwise share a blast radius.
    """

    def __init__(self, url: str | None = None, key: str | None = None):
        self._url = url
        self._key = key
        self._fernet = None

    @property
    def fernet(self):
        if self._fernet is None:
            from cryptography.fernet import Fernet

            key = self._key or os.environ.get("SOCIALQ_SECRET_KEY")
            if not key:
                load_dotenv()
                key = os.environ.get("SOCIALQ_SECRET_KEY")
            if not key:
                raise MissingSecret(
                    "SOCIALQ_SECRET_KEY is not set; generate one with "
                    "`socialq keygen` and add it to the runtime's secrets"
                )
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return self._fernet

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def get(self, ref: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM secrets WHERE ref = %s", (ref,)
            ).fetchone()
        if row is not None:
            return self.fernet.decrypt(bytes(row["value"])).decode()

        # Not stored yet: seed from the environment and write it in, so the
        # first deploy works from the runtime's own secrets and every later
        # read comes from the store that rotation can write to.
        seeded = os.environ.get(ref)
        if not seeded:
            load_dotenv()
            seeded = os.environ.get(ref)
        if not seeded:
            raise MissingSecret(ref)
        self.set(ref, seeded)
        return seeded

    def set(self, ref: str, value: str) -> None:
        token = self.fernet.encrypt(value.encode())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO secrets (ref, value) VALUES (%s, %s)"
                " ON CONFLICT (ref) DO UPDATE SET value = EXCLUDED.value,"
                " updated_at = now()",
                (ref, token),
            )

    def touch(self, refs) -> int:
        """No-op: rows do not expire. Present so callers need not care which
        store they were given."""
        return len(list(refs))

    def refs(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ref FROM secrets ORDER BY ref"
            ).fetchall()
        return [row["ref"] for row in rows]


class ModalSecretStore:
    """Writable secrets on Modal, backed by a persistent modal.Dict.

    Modal's own Secrets are immutable at runtime, and §8.3's token refresh has
    to write the new value back somewhere, so the Dict is the store and the
    Secret is only the seed: a ref not yet in the Dict is read from the
    environment and copied in on first use.

    THE HAZARD. A Dict entry expires after 7 days without a read or a write.
    Posting is bursty and may well go quiet for longer, and an expired entry
    means the Instagram token is gone -- unrecoverable without a browser round
    trip, which is exactly the failure §14 calls the most likely one. So
    `touch()` exists and the reconcile cron calls it every ten minutes,
    whether or not there is any work.
    """

    def __init__(self, dict_name: str = "socialq-secrets"):
        self.dict_name = dict_name
        self._dict = None

    @property
    def store(self):
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self.dict_name, create_if_missing=True)
        return self._dict

    def get(self, ref: str) -> str:
        value = self.store.get(ref)
        if value:
            return value
        # Not yet written: fall back to the seed in the Modal Secret.
        seeded = os.environ.get(ref)
        if not seeded:
            raise MissingSecret(ref)
        self.store[ref] = seeded
        return seeded

    def set(self, ref: str, value: str) -> None:
        self.store[ref] = value

    def touch(self, refs) -> int:
        """Read each ref so its Dict entry does not expire. See the hazard above."""
        alive = 0
        for ref in refs:
            try:
                self.get(ref)
                alive += 1
            except MissingSecret:
                pass
        return alive


class FileSecretStore:
    """A JSON file. For local development and tests.

    Not for production: it puts secrets on disk in plaintext. Modal's secret
    API is the intended implementation, added when the deploy target exists.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def get(self, ref: str) -> str:
        try:
            return self._read()[ref]
        except KeyError:
            raise MissingSecret(ref) from None

    def set(self, ref: str, value: str) -> None:
        data = self._read()
        data[ref] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
        self.path.chmod(0o600)
