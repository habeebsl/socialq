"""Media storage. §9.

Content-addressed by SHA-256 on R2, served from a custom domain. The platforms
fetch these URLs themselves on every publish, which is why R2's zero egress is
the point and why re-enqueueing a file must never re-upload it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .config import R2Config

_CHUNK = 1024 * 1024

# Explicit rather than mimetypes.guess_type: the platforms fetch by URL and
# reject or mis-handle a wrong Content-Type, so an unknown extension is an
# error at enqueue time (§10) rather than a surprise in the worker.
MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class UnsupportedMediaError(ValueError):
    """The file's extension has no Content-Type we are willing to serve."""


@dataclass(frozen=True)
class Media:
    """A row of the media table."""

    id: int
    project_id: str
    sha256: str
    url: str
    mime: str
    bytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def mime_for(path: Path) -> str:
    try:
        return MIME_BY_SUFFIX[path.suffix.lower()]
    except KeyError:
        raise UnsupportedMediaError(
            f"{path.name}: no known Content-Type for '{path.suffix}'"
        ) from None


def object_key(sha256: str, path: Path) -> str:
    """`media/<sha256><ext>` — §9."""
    return f"media/{sha256}{path.suffix.lower()}"


class MediaStore:
    """Uploads to R2 and hands back public URLs."""

    def __init__(self, config: R2Config, client=None):
        self.config = config
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3  # imported lazily: the SDK is not needed to read rows

            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.endpoint_url,
                aws_access_key_id=self.config.access_key_id,
                aws_secret_access_key=self.config.secret_access_key,
                region_name="auto",
            )
        return self._client

    def public_url(self, key: str) -> str:
        return f"{self.config.public_base_url}/{key}"

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] in (403, 404):
                return False
            raise
        return True

    def upload(self, path: Path, key: str, mime: str) -> str:
        """Put the file unless the key is already there. Returns its URL."""
        if not self.exists(key):
            with path.open("rb") as handle:
                self.client.put_object(
                    Bucket=self.config.bucket,
                    Key=key,
                    Body=handle,
                    ContentType=mime,
                )
        return self.public_url(key)


def ensure_media(
    conn: psycopg.Connection,
    project_id: str,
    path: Path,
    store: MediaStore,
) -> Media:
    """Return the media row for `path`, uploading it only if it is new.

    Hashing comes first and is local, so an already-known file costs one SELECT
    and no network at all.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"media not found: {path}")

    digest = sha256_file(path)
    row = conn.execute(
        "SELECT id, project_id, sha256, url, mime, bytes FROM media"
        " WHERE project_id = %s AND sha256 = %s",
        (project_id, digest),
    ).fetchone()
    if row is not None:
        return Media(**row)

    mime = mime_for(path)
    size = path.stat().st_size
    url = store.upload(path, object_key(digest, path), mime)

    # ON CONFLICT rather than a bare INSERT: two producers hashing the same file
    # concurrently both reach here, and the second must read back, not fail.
    row = conn.execute(
        "INSERT INTO media (project_id, sha256, url, mime, bytes)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (project_id, sha256) DO UPDATE SET url = EXCLUDED.url"
        " RETURNING id, project_id, sha256, url, mime, bytes",
        (project_id, digest, url, mime, size),
    ).fetchone()
    return Media(**row)
