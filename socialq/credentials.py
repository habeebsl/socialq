"""Credential rotation. §8.2.1 -- required, not optional.

The free tier is 20 credits/month, so a single account runs out. Three are held
and rotated.

THE CREDENTIAL IS A PAIR, NOT A KEY. Each PostPeer account connects its own
TikTok integration, so the accountId passed in platforms[].accountId belongs to
that account and is meaningless to the others. Swapping the API key without
swapping the account id will fail or, worse, post somewhere unintended -- so
they travel together in one row and are never assembled from separate places.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from .publishers.base import OutOfCreditError
from .secrets import MissingSecret, SecretStore

log = logging.getLogger("socialq.credentials")

# §8.2.1(1) says to refresh credits_left from GET /v1/usage/ when it is more
# than ~15 minutes stale. That does not survive contact with the budget: the
# call costs a credit itself, and a free account has 20 a month. Refreshing
# once per publish batch at 2 posts a day is ~60 credits a month -- three times
# the entire allowance, spent entirely on asking how much is left.
#
# So usage is NOT polled on the hot path. Credit is tracked locally instead:
# decrement on a successful publish, and let the API's own "insufficient
# credit" response mark a credential exhausted, which is free and authoritative.
# `fetch_usage` stays injectable for setup and manual checks.
USAGE_STALE_AFTER = timedelta(minutes=15)

# An unknown balance means untried, not empty. A credential whose count has
# never been read must be attempted -- the API will say no for free if it is
# actually out, and that answer is better than one we guessed.
UNKNOWN = None


@dataclass
class Credential:
    id: int
    project_id: str
    publisher: str
    label: str
    api_key_ref: str
    account_ids: dict[str, str]
    credits_left: int | None = None
    checked_at: datetime | None = None
    enabled: bool = True
    # The id THIS credential uses for the account it was looked up against,
    # from publisher_account_ids. Set when selected for a specific account.
    resolved_id: str | None = None

    def account_for(self, platform: str) -> str | None:
        """DEPRECATED: the JSON holds one id per platform, which cannot express
        rotation. Prefer `resolved_id` from a per-account lookup."""
        return self.resolved_id or self.account_ids.get(platform)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CredentialPool:
    """Picks which PostPeer account publishes, and keeps its credit count."""

    def __init__(
        self,
        conn: psycopg.Connection,
        secrets: SecretStore,
        *,
        publisher: str = "postpeer",
        fetch_usage=None,
        now=utcnow,
        stale_after: timedelta = USAGE_STALE_AFTER,
    ):
        self.conn = conn
        self.secrets = secrets
        self.publisher = publisher
        # Injected so selection can be tested without spending credits, and so
        # the pool does not depend on the publisher that uses it.
        self.fetch_usage = fetch_usage
        self.now = now
        self.stale_after = stale_after

    # -- reading -----------------------------------------------------------

    def all_for_account(self, account_id: int) -> list[Credential]:
        """The credentials that can post to this account, with the id each one
        uses for it.

        §8.2.1: rotation means several credentials serve one account, and each
        holds a different id for it because each authorised the platform
        separately. A credential with no row here simply cannot reach that
        account -- which is information, not an error.
        """
        rows = self.conn.execute(
            "SELECT c.id, c.project_id, c.publisher, c.label, c.api_key_ref,"
            " c.account_ids, c.credits_left, c.checked_at, c.enabled,"
            " m.external_id AS resolved_id"
            " FROM publisher_credentials c"
            " JOIN publisher_account_ids m ON m.credential_id = c.id"
            " WHERE m.account_id = %s AND c.publisher = %s AND c.enabled"
            " ORDER BY c.id",
            (account_id, self.publisher),
        ).fetchall()
        credentials = []
        for row in rows:
            resolved = row.pop("resolved_id")
            credential = Credential(**row)
            credential.resolved_id = resolved
            credentials.append(credential)
        return credentials

    def select_for_account(self, account_id: int, label: str = "") -> Credential:
        """§8.2.1(2): of the credentials that can serve this account, the one
        with the MOST credits remaining."""
        candidates = self.all_for_account(account_id)
        if not candidates:
            raise LookupError(
                f"no enabled {self.publisher} credential is connected to "
                f"account {label or account_id}; connect it in the dashboard "
                "and re-register"
            )

        for credential in candidates:
            self.reset_if_new_month(credential)
            self.refresh_if_stale(credential)

        usable = [c for c in candidates
                  if c.credits_left is UNKNOWN or c.credits_left > 0]
        if not usable:
            raise OutOfCreditError(
                f"all {len(candidates)} {self.publisher} credentials for "
                f"{label or account_id} are exhausted"
            )
        return max(usable, key=lambda c: (c.credits_left is not UNKNOWN,
                                          c.credits_left or 0))

    def all_for(self, project_id: str, platform: str) -> list[Credential]:
        rows = self.conn.execute(
            "SELECT id, project_id, publisher, label, api_key_ref, account_ids,"
            " credits_left, checked_at, enabled FROM publisher_credentials"
            " WHERE project_id = %s AND publisher = %s AND enabled",
            (project_id, self.publisher),
        ).fetchall()
        return [
            c for c in (Credential(**row) for row in rows) if c.account_for(platform)
        ]

    def select(self, project_id: str, platform: str) -> Credential:
        """§8.2.1(2): the enabled credential with the MOST credits remaining.

        Most, not first-with-any: it spreads load and leaves headroom for
        retries.
        """
        candidates = self.all_for(project_id, platform)
        if not candidates:
            raise LookupError(
                f"no {self.publisher} credential for {project_id}/{platform}"
            )

        for credential in candidates:
            self.reset_if_new_month(credential)
            self.refresh_if_stale(credential)

        usable = [c for c in candidates if c.credits_left is UNKNOWN
                  or c.credits_left > 0]
        if not usable:
            # §8.2.1(3): do not fail the target. The worker turns this into a
            # wait until the credits reset.
            raise OutOfCreditError(
                f"all {len(candidates)} {self.publisher} credentials are exhausted"
            )
        # An unknown balance sorts last, so a credential with known credit is
        # preferred over a guess -- but a guess still beats not posting.
        return max(usable, key=lambda c: (c.credits_left is not UNKNOWN,
                                          c.credits_left or 0))

    def reset_if_new_month(self, credential: Credential) -> None:
        """Credits reset monthly, so a count from last month is meaningless.

        Without this an exhausted credential stays at zero for ever and the
        rotation permanently loses an account.
        """
        checked = credential.checked_at
        if checked is None:
            return
        now = self.now()
        if (checked.year, checked.month) < (now.year, now.month):
            credential.credits_left = UNKNOWN
            credential.checked_at = None
            self.conn.execute(
                "UPDATE publisher_credentials SET credits_left = NULL,"
                " checked_at = NULL WHERE id = %s",
                (credential.id,),
            )
            self.conn.commit()
            log.info("credential %s: new month, credit count reset",
                     credential.label)

    def api_key(self, credential: Credential) -> str:
        try:
            return self.secrets.get(credential.api_key_ref)
        except MissingSecret:
            # A row pointing at a secret that is not there is worse than no row:
            # it will be selected forever and fail every time.
            self.disable(credential, "secret missing")
            raise

    # -- credit accounting -------------------------------------------------

    def refresh_if_stale(self, credential: Credential) -> None:
        if self.fetch_usage is None:
            return
        checked = credential.checked_at
        if checked is not None and self.now() - checked < self.stale_after:
            return
        try:
            remaining = self.fetch_usage(credential)
        except OutOfCreditError:
            remaining = 0
        except Exception as exc:  # noqa: BLE001 -- a usage check must never
            # stop a publish; the stale count is better than no count.
            log.warning("usage check failed for %s: %s", credential.label, exc)
            return
        self._store_credits(credential, remaining)

    def spend(self, credential: Credential, credits: int = 1) -> None:
        """Decrement locally after a successful publish.

        Cheaper and more current than re-reading usage, which would itself cost
        a credit. The next refresh corrects any drift.
        """
        if credential.credits_left is None:
            return
        self._store_credits(credential, max(0, credential.credits_left - credits), touch=False)

    def _store_credits(
        self, credential: Credential, remaining: int, *, touch: bool = True
    ) -> None:
        credential.credits_left = remaining
        if touch:
            credential.checked_at = self.now()
        self.conn.execute(
            "UPDATE publisher_credentials SET credits_left = %s"
            + (", checked_at = now()" if touch else "")
            + " WHERE id = %s",
            (remaining, credential.id),
        )
        self.conn.commit()

    def exhausted(self, credential: Credential) -> None:
        """A publish failed for want of credit despite the cached count.

        §8.2.1: treat that as "try the next credential", not as a retry of the
        same one.
        """
        self._store_credits(credential, 0)

    def disable(self, credential: Credential, reason: str) -> None:
        """§8.2.1: a revoked key or closed account. One dead account must never
        stall the queue."""
        credential.enabled = False
        self.conn.execute(
            "UPDATE publisher_credentials SET enabled = false WHERE id = %s",
            (credential.id,),
        )
        self.conn.commit()
        log.error("ALERT credential %s disabled: %s", credential.label, reason)
