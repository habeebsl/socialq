"""Every query the worker runs, in one file.

Kept apart from worker.py so the state machine reads as a state machine and the
SQL can be reviewed as SQL -- §5's claim in particular, which is the one query
whose exact shape is load-bearing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import psycopg

from .media import Media
from .models import Account, Post, Target, TargetState

# §5. FOR UPDATE SKIP LOCKED is a correct claim-and-lock at orders of magnitude
# beyond this workload, and keeps the history in the same transaction as the
# work. Scaling out is running more workers; there is no coordination.
CLAIM_SQL = """
UPDATE targets t SET state = 'claimed', claimed_at = now(), claimed_by = %(worker)s
WHERE t.id IN (
  SELECT t2.id
  FROM targets t2
  JOIN posts p ON p.id = t2.post_id
  JOIN accounts a ON a.id = t2.account_id
  WHERE t2.state = 'pending'
    AND a.enabled
    AND p.scheduled_for <= now()
    AND (t2.next_attempt_at IS NULL OR t2.next_attempt_at <= now())
  ORDER BY p.scheduled_for
  FOR UPDATE SKIP LOCKED
  LIMIT %(limit)s
)
RETURNING t.*
"""


def _target(row: dict) -> Target:
    return Target(**row)


def claim(conn: psycopg.Connection, worker: str, limit: int = 10) -> list[Target]:
    """Take up to `limit` due targets for this worker."""
    rows = conn.execute(CLAIM_SQL, {"worker": worker, "limit": limit}).fetchall()
    return [_target(row) for row in rows]


def load_post(conn: psycopg.Connection, post_id: int) -> Post:
    row = conn.execute(
        "SELECT id, project_id, external_id, pipeline, caption, cta, hashtags,"
        " media_ids, aigc, scheduled_for FROM posts WHERE id = %s",
        (post_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"post {post_id} not found")
    return Post(**row)


def load_account(conn: psycopg.Connection, account_id: int) -> Account:
    row = conn.execute(
        "SELECT id, project_id, name, platform, handle, publisher, external_id,"
        " enabled FROM accounts WHERE id = %s",
        (account_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"account {account_id} not found")
    return Account(**row)


def load_media(conn: psycopg.Connection, media_ids: list[int]) -> list[Media]:
    """Media for a post, in the order the post lists them.

    Order is the carousel's order, so it cannot be left to the database.
    """
    if not media_ids:
        return []
    rows = conn.execute(
        "SELECT id, project_id, sha256, url, mime, bytes FROM media"
        " WHERE id = ANY(%s)",
        (media_ids,),
    ).fetchall()
    by_id = {row["id"]: Media(**row) for row in rows}
    missing = [mid for mid in media_ids if mid not in by_id]
    if missing:
        raise LookupError(f"media not found: {missing}")
    return [by_id[mid] for mid in media_ids]


def begin_attempt(
    conn: psycopg.Connection,
    target: Target,
    request: dict[str, Any] | None = None,
    credential_id: int | None = None,
) -> int:
    """§6 step 2: record the attempt and go in_flight, then COMMIT.

    Committing before the network call is what makes recovery possible. Without
    it, a crash between the call and the record is indistinguishable from a call
    that never happened.
    """
    n = target.attempts + 1
    row = conn.execute(
        "INSERT INTO attempts (target_id, n, idem_key, request, credential_id)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (target.id, n, f"target:{target.id}:{n}", json.dumps(request or {}),
         credential_id),
    ).fetchone()
    conn.execute(
        "UPDATE targets SET state = 'in_flight' WHERE id = %s", (target.id,)
    )
    conn.commit()
    return row["id"]


def finish_attempt(
    conn: psycopg.Connection, attempt_id: int, response: dict[str, Any]
) -> None:
    conn.execute(
        "UPDATE attempts SET response = %s, finished_at = now() WHERE id = %s",
        (json.dumps(response, default=str), attempt_id),
    )


def latest_attempt(conn: psycopg.Connection, target_id: int) -> dict | None:
    """The most recent attempt, whose started_at bounds the reconcile window."""
    return conn.execute(
        "SELECT * FROM attempts WHERE target_id = %s ORDER BY n DESC LIMIT 1",
        (target_id,),
    ).fetchone()


def mark_published(
    conn: psycopg.Connection,
    target: Target,
    provider_post_id: str,
    permalink: str | None,
) -> None:
    conn.execute(
        "UPDATE targets SET state = 'published', provider_post_id = %s,"
        " permalink = %s, attempts = attempts + 1, last_error = NULL,"
        " next_attempt_at = NULL WHERE id = %s",
        (provider_post_id, permalink, target.id),
    )


def mark_for_retry(
    conn: psycopg.Connection, target: Target, error: str, next_attempt_at: datetime
) -> None:
    """Back to pending, one attempt spent."""
    conn.execute(
        "UPDATE targets SET state = 'pending', attempts = attempts + 1,"
        " next_attempt_at = %s, last_error = %s, claimed_at = NULL,"
        " claimed_by = NULL WHERE id = %s",
        (next_attempt_at, error[:2000], target.id),
    )


def mark_waiting(
    conn: psycopg.Connection, target: Target, reason: str, next_attempt_at: datetime
) -> None:
    """Back to pending without spending an attempt.

    §8.2.1(3): running out of credit is a wait, not an error. The same applies
    to a platform rate limit -- neither says anything is wrong with the post.
    """
    conn.execute(
        "UPDATE targets SET state = 'pending', next_attempt_at = %s,"
        " last_error = %s, claimed_at = NULL, claimed_by = NULL WHERE id = %s",
        (next_attempt_at, reason[:2000], target.id),
    )


def mark_dead(conn: psycopg.Connection, target: Target, error: str) -> None:
    conn.execute(
        "UPDATE targets SET state = 'dead', attempts = attempts + 1,"
        " last_error = %s, next_attempt_at = NULL WHERE id = %s",
        (error[:2000], target.id),
    )


def disable_account(conn: psycopg.Connection, account_id: int) -> None:
    conn.execute("UPDATE accounts SET enabled = false WHERE id = %s", (account_id,))


def stale_in_flight(conn: psycopg.Connection, older_than: datetime) -> list[Target]:
    """§7: in_flight targets a dying worker left behind."""
    rows = conn.execute(
        "SELECT * FROM targets WHERE state = 'in_flight' AND claimed_at < %s",
        (older_than,),
    ).fetchall()
    return [_target(row) for row in rows]


def release_claimed(conn: psycopg.Connection, older_than: datetime) -> int:
    """Return targets stuck in `claimed` to pending.

    A crash between claiming and the attempt insert leaves a row nothing will
    ever pick up. That gap is safe to reopen: no network call has happened yet,
    which is exactly what distinguishes `claimed` from `in_flight`.
    """
    result = conn.execute(
        "UPDATE targets SET state = 'pending', claimed_at = NULL, claimed_by = NULL"
        " WHERE state = 'claimed' AND claimed_at < %s",
        (older_than,),
    )
    return result.rowcount


__all__ = [
    "TargetState",
    "claim",
    "load_post",
    "load_account",
    "load_media",
    "begin_attempt",
    "finish_attempt",
    "latest_attempt",
    "mark_published",
    "mark_for_retry",
    "mark_waiting",
    "mark_dead",
    "disable_account",
    "stale_in_flight",
    "release_claimed",
]
