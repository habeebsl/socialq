"""Reconciliation. §7 -- the part that makes "no manual steps" true.

A worker that dies mid-publish leaves an `in_flight` row. Retrying blindly
double-posts. Asking a human defeats the purpose. So query the platform.

This is code, not a notification. Do not ship a version that emails you
about it.

MATCHING. §7 says to match PostPeer on the media URL, which works because
PostPeer echoes back the URL we submitted. Instagram does not: it re-hosts the
media on its own CDN, so `media_url` from the platform never equals our R2 URL.
The identifying field there is the caption, which is ours and exact. A post is
ours when the window matches AND (the media URL matches OR the caption does),
so a publisher only needs to supply one of the two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from . import repo
from .models import RemotePost, Target
from .publishers.base import AuthError, Publisher, PublishError
from .text import text_for
from .worker import MAX_ATTEMPTS, backoff, utcnow

log = logging.getLogger("socialq.reconcile")

# §7: "every in_flight target older than ~30 minutes". Long enough that a
# publish still in progress is not mistaken for a dead worker.
STALE_AFTER = timedelta(minutes=30)

# The platform's clock is not ours, and a container can be created minutes
# before the post appears.
WINDOW_SLACK = timedelta(minutes=10)

# §8.3: a token must be refreshed well inside its 60 days. At 14 days' margin
# a fortnight of failed refreshes still leaves room to act.
REFRESH_WHEN_WITHIN = timedelta(days=14)


@dataclass
class Stats:
    checked: int = 0
    recovered: int = 0
    requeued: int = 0
    released: int = 0
    dead: int = 0
    errors: list[str] = field(default_factory=list)


class Reconciler:
    """Resolves in_flight targets against the platform."""

    def __init__(
        self,
        conn: psycopg.Connection,
        resolve,
        *,
        stale_after: timedelta = STALE_AFTER,
        max_attempts: int = MAX_ATTEMPTS,
        now=utcnow,
    ):
        self.conn = conn
        self.resolve = resolve
        self.stale_after = stale_after
        self.max_attempts = max_attempts
        self.now = now

    def run_once(self) -> Stats:
        stats = Stats()
        now = self.now()

        # A target stuck in `claimed` never reached the network, so it is
        # simply reopened. Only `in_flight` needs the platform asked.
        stats.released = repo.release_claimed(self.conn, now - self.stale_after)
        self.conn.commit()

        for target in repo.stale_in_flight(self.conn, now - self.stale_after):
            stats.checked += 1
            try:
                self.reconcile_one(target, stats)
            except Exception as exc:  # noqa: BLE001 -- one bad target must not
                # take the sweep down with it.
                log.exception("target %s: reconcile failed", target.id)
                stats.errors.append(f"target {target.id}: {exc}")
                self.conn.rollback()
        return stats

    def reconcile_one(self, target: Target, stats: Stats) -> None:
        post = repo.load_post(self.conn, target.post_id)
        account = repo.load_account(self.conn, target.account_id)
        media = repo.load_media(self.conn, post.media_ids)
        publisher: Publisher = self.resolve(account)

        attempt = repo.latest_attempt(self.conn, target.id)
        started = attempt["started_at"] if attempt else target.claimed_at
        since = (started or self.now()) - WINDOW_SLACK

        try:
            remote = publisher.find_recent(account, since)
        except PublishError as exc:
            # Cannot tell either way. Leave it in_flight: guessing here is
            # exactly the double-post this function exists to prevent.
            log.warning("target %s: cannot reach %s: %s", target.id, account.platform, exc)
            stats.errors.append(f"target {target.id}: {exc}")
            if isinstance(exc, AuthError):
                self.alert(f"reconcile cannot reach {account.platform}: {exc}")
            return

        expected = text_for(post, account.platform)
        urls = {item.url for item in media}
        match = find_match(remote, urls, expected, since)

        if match is not None:
            if attempt:
                repo.finish_attempt(
                    self.conn,
                    attempt["id"],
                    {"ok": True, "reconciled": True, "id": match.provider_post_id},
                )
            repo.mark_published(
                self.conn, target, match.provider_post_id, match.permalink
            )
            self.conn.commit()
            stats.recovered += 1
            log.info(
                "target %s was already published as %s; recovered",
                target.id, match.provider_post_id,
            )
            return

        # Genuinely not there: the call never landed. Spend the attempt -- it
        # was a real one -- and let the worker try again.
        error = "in_flight with no post found on the platform"
        if attempt:
            repo.finish_attempt(
                self.conn, attempt["id"], {"ok": False, "error": error}
            )
        if target.attempts + 1 >= self.max_attempts:
            repo.mark_dead(self.conn, target, error)
            self.conn.commit()
            stats.dead += 1
            self.alert(f"target {target.id} dead after reconcile: {error}")
            return

        repo.mark_for_retry(
            self.conn, target, error, self.now() + backoff(target.attempts + 1)
        )
        self.conn.commit()
        stats.requeued += 1
        log.info("target %s not found on platform; requeued", target.id)

    def alert(self, message: str) -> None:
        log.error("ALERT %s", message)


def find_match(
    remote: list[RemotePost],
    media_urls: set[str],
    expected_text: str,
    since: datetime,
) -> RemotePost | None:
    """The post among `remote` that is ours, or None.

    Both identifiers are exact -- a URL we generated or a caption we assembled
    -- so this never guesses. An unmatched post means retry, which is the safe
    direction only because the match itself is strict.
    """
    for item in remote:
        if item.created_at is not None and item.created_at < since:
            continue
        if item.media_url and item.media_url in media_urls:
            return item
        if expected_text and item.caption and item.caption.strip() == expected_text:
            return item
    return None


def due_for_refresh(expires_at: datetime | None, now: datetime) -> bool:
    """§8.3: refresh on a schedule, well before the 60 days run out."""
    if expires_at is None:
        return True
    return expires_at - now <= REFRESH_WHEN_WITHIN


def refresh_tokens(conn: psycopg.Connection, resolve_credential, now=utcnow) -> list[str]:
    """Refresh every Instagram token approaching expiry. §8.3, §14.

    Returns the labels refreshed. A failure here is the loudest thing socialq
    has: §14 calls token expiry the most likely production failure, and it is
    the one that breaks with no code change.
    """
    refreshed = []
    rows = conn.execute(
        "SELECT id, project_id, publisher, label, api_key_ref, expires_at"
        " FROM publisher_credentials"
        " WHERE publisher = 'instagram_graph' AND enabled"
    ).fetchall()

    for row in rows:
        if not due_for_refresh(row["expires_at"], now()):
            continue
        publisher, store = resolve_credential(row)
        try:
            token, expires_at = publisher.refresh_token()
        except PublishError as exc:
            log.error("ALERT token refresh failed for %s: %s", row["label"], exc)
            continue
        store.set(row["api_key_ref"], token)
        conn.execute(
            "UPDATE publisher_credentials SET expires_at = %s, refreshed_at = now()"
            " WHERE id = %s",
            (expires_at, row["id"]),
        )
        conn.commit()
        refreshed.append(row["label"])
        log.info("refreshed %s, valid until %s", row["label"], expires_at)
    return refreshed


def utcnow_() -> datetime:  # pragma: no cover - re-exported for symmetry
    return datetime.now(timezone.utc)
