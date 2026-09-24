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


def upload(
    path: Path | str,
    *,
    project: str,
    conn: psycopg.Connection | None = None,
    store: "MediaStore | None" = None,
) -> str:
    """Store a file and register it. Returns its public URL. Idempotent.

    Producers call this at render time, long before anything is published, so
    a rendered-but-unpublished video has a URL to preview and a row that says
    it still exists. Before this existed, producers reimplemented the key
    scheme and the skip-if-present upload themselves -- and a second
    implementation of a storage scheme drifts silently rather than failing.

    `project` is required. An object stored without a media row is invisible
    to the pruner (§9 works from rows), so it would occupy the bucket for ever
    with nothing able to reclaim it -- a worse version of the orphan problem
    the prune rules exist to solve.
    """
    from .db import connect

    if store is None:
        store = MediaStore(R2Config.from_env())

    def register(connection) -> str:
        # Render-time upload is the FIRST thing a producer does, before any
        # enqueue has created the project row, so this cannot assume one.
        connection.execute(
            "INSERT INTO projects (id) VALUES (%s) ON CONFLICT DO NOTHING",
            (project,),
        )
        return ensure_media(connection, project, Path(path), store).url

    if conn is not None:
        return register(conn)
    with connect() as owned:
        return register(owned)


def media_for_url(
    conn: psycopg.Connection, project_id: str, url: str
) -> Media | None:
    """The row for an already-registered URL, or None.

    Looked up by the digest embedded in the key rather than by the URL string:
    `(project_id, sha256)` is the table's unique index, so this is both exact
    and free. The bytes are never fetched -- a producer passing a URL is
    asserting it registered the object, and downloading 60MB to verify that
    defeats the purpose of passing a URL at all.
    """
    digest = sha256_from_url(url)
    if digest is None:
        return None
    row = conn.execute(
        "SELECT id, project_id, sha256, url, mime, bytes FROM media"
        " WHERE project_id = %s AND sha256 = %s",
        (project_id, digest),
    ).fetchone()
    return Media(**row) if row else None


def sha256_from_url(url: str) -> str | None:
    """The digest out of a socialq media URL, or None if it is not one."""
    name = url.rsplit("/", 1)[-1]
    digest = name.split(".", 1)[0]
    if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
        return digest
    return None


def is_url(value) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


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
