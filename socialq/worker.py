"""The worker. §6.

    1. claim                    state -> claimed
    2. INSERT attempts row      idem_key = target:<id>:<n>, COMMIT
       state -> in_flight       (committed BEFORE the network call)
    3. call the publisher
    4. on success: record provider_post_id + permalink, state -> published
       on failure: state -> pending, attempts += 1,
                   next_attempt_at = now() + backoff(attempts)
                   after N attempts -> dead, and alert

Step 2 committing before step 3 is the whole design. Everything else here is
bookkeeping around that one ordering.
"""

from __future__ import annotations

import logging
import os
import random
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from . import repo
from .models import Target
from .publishers.base import (
    AuthError,
    OutOfCreditError,
    Publisher,
    PublishError,
    RateLimitedError,
)

log = logging.getLogger("socialq.worker")

# §6: exponential with jitter, min(2^n minutes, 6h), cap at 6 attempts.
MAX_ATTEMPTS = 6
BACKOFF_CAP = timedelta(hours=6)
JITTER = 0.2


def backoff(attempts: int) -> timedelta:
    """Delay before attempt `attempts + 1`. Jittered so retries do not
    synchronise across targets that failed together."""
    base = min(timedelta(minutes=2**attempts), BACKOFF_CAP)
    spread = base * JITTER * random.random()
    return base + spread


def next_month(now: datetime) -> datetime:
    """Midnight UTC on the first of next month.

    §8.2.1(3): when every credential is exhausted the target waits for the
    credits to reset rather than failing.
    """
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Stats:
    claimed: int = 0
    published: int = 0
    retried: int = 0
    waiting: int = 0
    dead: int = 0
    errors: list[str] = field(default_factory=list)


class Worker:
    """Claims due work and publishes it.

    One instance per process. `resolve` maps an account to the Publisher that
    serves it -- the worker never learns which vendor is behind one (§3).
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        resolve,
        *,
        worker_id: str | None = None,
        batch: int = 10,
        max_attempts: int = MAX_ATTEMPTS,
        now=utcnow,
    ):
        self.conn = conn
        self.resolve = resolve
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.batch = batch
        self.max_attempts = max_attempts
        self.now = now

    def run_once(self) -> Stats:
        """One pass: claim a batch, publish each, record each."""
        stats = Stats()
        targets = repo.claim(self.conn, self.worker_id, self.batch)
        self.conn.commit()
        stats.claimed = len(targets)

        for target in targets:
            try:
                self.publish_one(target, stats)
            except Exception as exc:  # noqa: BLE001 -- one bad target must not
                # take the batch down with it.
                log.exception("target %s: unhandled error", target.id)
                stats.errors.append(f"target {target.id}: {exc}")
                self.conn.rollback()
                self._retry(target, f"unhandled: {exc}", stats)
        return stats

    def publish_one(self, target: Target, stats: Stats) -> None:
        post = repo.load_post(self.conn, target.post_id)
        account = repo.load_account(self.conn, target.account_id)
        media = repo.load_media(self.conn, post.media_ids)

        try:
            # Resolving can fail for reasons that are not this target's fault:
            # §8.2.1 selects a credential here, and "every account is out of
            # credit" must become a wait, not a spent attempt.
            publisher: Publisher = self.resolve(account)
        except PublishError as exc:
            self._handle_failure(target, account, exc, None, stats)
            return

        request = {
            "publisher": publisher.name,
            "platform": account.platform,
            "surface": target.surface,
            "media": [item.url for item in media],
        }
        # §6 step 2 -- this commits.
        attempt_id = repo.begin_attempt(
            self.conn, target, request, getattr(publisher, "credential_id", None)
        )

        try:
            result = publisher.publish(target, post, media)
        except PublishError as exc:
            self._handle_failure(target, account, exc, attempt_id, stats)
            return

        repo.finish_attempt(self.conn, attempt_id, {"ok": True, **result.raw})
        repo.mark_published(
            self.conn, target, result.provider_post_id, result.permalink
        )
        self.conn.commit()
        stats.published += 1
        log.info(
            "published target %s to %s as %s",
            target.id, account.platform, result.provider_post_id,
        )

    # -- failure handling --------------------------------------------------

    def _handle_failure(
        self,
        target: Target,
        account,
        exc: PublishError,
        attempt_id: int | None,
        stats: Stats,
    ) -> None:
        # attempt_id is None when the failure happened before an attempt was
        # recorded -- nothing was sent, so there is nothing to close.
        if attempt_id is not None:
            repo.finish_attempt(
                self.conn, attempt_id, {"ok": False, "error": str(exc), "raw": exc.raw}
            )

        if isinstance(exc, OutOfCreditError):
            # §8.2.1(3): a wait, not an error. No attempt is spent.
            repo.mark_waiting(self.conn, target, str(exc), next_month(self.now()))
            self.conn.commit()
            stats.waiting += 1
            log.warning("target %s waiting on credit until next month", target.id)
            return

        if isinstance(exc, RateLimitedError):
            # The platform's ceiling says nothing about this post.
            retry_at = exc.retry_after or (self.now() + timedelta(hours=1))
            repo.mark_waiting(self.conn, target, str(exc), retry_at)
            self.conn.commit()
            stats.waiting += 1
            log.warning("target %s rate limited until %s", target.id, retry_at)
            return

        if isinstance(exc, AuthError):
            # §8.2.1: one dead credential must never stall the queue. Disable
            # the account so the claim query stops selecting it, and alert --
            # §14 calls token expiry the most likely production failure.
            repo.disable_account(self.conn, account.id)
            repo.mark_for_retry(
                self.conn, target, str(exc), self.now() + BACKOFF_CAP
            )
            self.conn.commit()
            stats.retried += 1
            self.alert(
                f"account {account.name} ({account.platform}) disabled: {exc}"
            )
            return

        self._retry(target, str(exc), stats)

    def _retry(self, target: Target, error: str, stats: Stats) -> None:
        if target.attempts + 1 >= self.max_attempts:
            repo.mark_dead(self.conn, target, error)
            self.conn.commit()
            stats.dead += 1
            self.alert(f"target {target.id} dead after {target.attempts + 1}: {error}")
            return

        retry_at = self.now() + backoff(target.attempts + 1)
        repo.mark_for_retry(self.conn, target, error, retry_at)
        self.conn.commit()
        stats.retried += 1
        log.warning("target %s failed, retrying at %s: %s", target.id, retry_at, error)

    def alert(self, message: str) -> None:
        """Loud failures. §14 -- a refresh failure must not be quiet.

        Logging at ERROR is the v1 implementation; Modal surfaces those.
        """
        log.error("ALERT %s", message)
