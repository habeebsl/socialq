"""Media pruning. §9.

R2's free tier is 10 GB -- roughly 160 videos at current sizes. Prune on a
schedule rather than discovering the ceiling at 90%.

The rule is conservative on purpose: an object is deleted only once every
target that could still need it has finished. A pending or in_flight target
means the platform may yet fetch that URL, and deleting it would turn a retry
into a permanent failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

log = logging.getLogger("socialq.prune")

# Long enough that reconciliation (§7) has certainly finished with the URL, and
# that a human looking into a bad post can still see what was sent.
KEEP_FOR = timedelta(days=7)

# Media whose targets are all resolved: published, or dead and never coming
# back. A `dead` target will not be retried, so its media is safe to drop.
PRUNABLE_SQL = """
SELECT m.id, m.project_id, m.url, m.sha256, m.bytes
FROM media m
WHERE m.pruned_at IS NULL
  AND m.created_at < %(cutoff)s
  AND EXISTS (
    SELECT 1 FROM posts p WHERE m.id = ANY(p.media_ids)
  )
  AND NOT EXISTS (
    SELECT 1
    FROM posts p
    JOIN targets t ON t.post_id = p.id
    WHERE m.id = ANY(p.media_ids)
      AND t.state NOT IN ('published', 'dead')
  )
ORDER BY m.bytes DESC
LIMIT %(limit)s
"""


@dataclass
class Stats:
    pruned: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def key_for(url: str) -> str:
    """The object key from a public URL: everything after the domain."""
    return url.split("/", 3)[-1]


def prune(
    conn: psycopg.Connection,
    store,
    *,
    keep_for: timedelta = KEEP_FOR,
    limit: int = 100,
    now=utcnow,
    dry_run: bool = False,
) -> Stats:
    """Delete R2 objects whose posts are all finished with them."""
    stats = Stats()
    rows = conn.execute(
        PRUNABLE_SQL, {"cutoff": now() - keep_for, "limit": limit}
    ).fetchall()

    for row in rows:
        key = key_for(row["url"])
        if dry_run:
            log.info("would prune %s (%.1f MB)", key, row["bytes"] / 1e6)
            stats.pruned += 1
            stats.bytes_freed += row["bytes"]
            continue
        try:
            store.client.delete_object(Bucket=store.config.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 -- one bad object must not stop
            # the sweep; the row stays unpruned and is retried next run.
            log.warning("could not delete %s: %s", key, exc)
            stats.errors.append(f"{key}: {exc}")
            continue

        conn.execute(
            "UPDATE media SET pruned_at = now() WHERE id = %s", (row["id"],)
        )
        conn.commit()
        stats.pruned += 1
        stats.bytes_freed += row["bytes"]
        log.info("pruned %s (%.1f MB)", key, row["bytes"] / 1e6)

    return stats


def usage(conn: psycopg.Connection) -> dict:
    """What is still taking up space, for the alert threshold."""
    row = conn.execute(
        "SELECT count(*) AS objects, coalesce(sum(bytes), 0) AS bytes"
        " FROM media WHERE pruned_at IS NULL"
    ).fetchone()
    # sum() over bigint returns numeric, which psycopg hands back as Decimal --
    # and Decimal / float raises TypeError, so the nightly prune job would have
    # crashed on its own report line. Cast here rather than at each caller.
    return {"objects": int(row["objects"]), "bytes": int(row["bytes"])}
