"""Environment-derived configuration.

The environment is the single source of truth: Modal injects secrets as env
vars and so does the producer's shell. `.env` is a local convenience that fills
in gaps in that environment -- it never overrides a variable already set, so a
deployed process cannot be surprised by a file that shipped by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=value lines into os.environ, without clobbering what is set."""
    path = path or DOTENV_PATH
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        load_dotenv()
        value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def database_url() -> str:
    return _require("DATABASE_URL")


@dataclass(frozen=True)
class R2Config:
    """Cloudflare R2. §9 — content-addressed, served from a custom domain."""

    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    @classmethod
    def from_env(cls) -> R2Config:
        base = _require("R2_PUBLIC_BASE_URL").rstrip("/")
        if ".r2.dev" in base:
            # §9: r2.dev is rate-limited and documented as unsuitable for
            # production. The platforms fetch this URL on every publish.
            raise ConfigError(
                "R2_PUBLIC_BASE_URL points at an r2.dev subdomain; "
                "use a custom domain (media.<product-domain>)"
            )
        return cls(
            account_id=_require("R2_ACCOUNT_ID"),
            access_key_id=_require("R2_ACCESS_KEY_ID"),
            secret_access_key=_require("R2_SECRET_ACCESS_KEY"),
            bucket=_require("R2_BUCKET"),
            public_base_url=base,
        )
