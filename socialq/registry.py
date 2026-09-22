"""Routing. §8.4 -- `accounts.publisher` decides.

The worker asks for "the publisher for this account" and gets one. It never
learns that TikTok goes through a vendor and Instagram does not; that is the
boundary §3 exists to protect.

Suggested routing, from §8.4:
    tiktok    -> postpeer
    instagram -> instagram_graph   (free, and uncapped by credits)
    x         -> manual. NEVER through PostPeer: 5 credits a post and 50 for
                 one containing a URL, which is two and a half months of a free
                 account for a single post.
"""

from __future__ import annotations

import psycopg

from .credentials import CredentialPool
from .models import Account
from .publishers.base import Publisher
from .publishers.instagram import InstagramGraph
from .publishers.postpeer import PostPeerTikTok
from .secrets import SecretStore

# §2/§14. Routing X through PostPeer is a costly mistake that looks like a
# one-line config change, so it is refused in code rather than documented.
FORBIDDEN = {("x", "postpeer"), ("twitter", "postpeer")}


class UnknownPublisher(LookupError):
    pass


class Registry:
    """Builds the publisher for an account. Callable, so it is the worker's
    `resolve` directly."""

    def __init__(
        self,
        conn: psycopg.Connection,
        secrets: SecretStore,
        *,
        pool: CredentialPool | None = None,
        draft: bool = False,
    ):
        self.conn = conn
        self.secrets = secrets
        self.pool = pool or CredentialPool(conn, secrets)
        self.draft = draft

    def __call__(self, account: Account) -> Publisher:
        if (account.platform, account.publisher) in FORBIDDEN:
            raise UnknownPublisher(
                f"refusing to route {account.platform} through {account.publisher}: "
                "see §2 on credit cost"
            )
        if account.publisher == "instagram_graph":
            return self._instagram(account)
        if account.publisher == "postpeer":
            return self._postpeer(account)
        raise UnknownPublisher(f"no publisher named {account.publisher!r}")

    def _instagram(self, account: Account) -> InstagramGraph:
        credential = self._credential_row(account, "instagram_graph")
        token = self.secrets.get(credential["api_key_ref"])
        ig_user_id = (
            credential.get("resolved_id")
            or account.external_id
            # Legacy rows: the deprecated JSON, for credentials registered
            # before publisher_account_ids existed.
            or (credential.get("account_ids") or {}).get("instagram")
        )
        if not ig_user_id:
            raise UnknownPublisher(f"no instagram user id for account {account.name}")
        return InstagramGraph(token, str(ig_user_id))

    def _postpeer(self, account: Account) -> PostPeerTikTok:
        # §8.2.1: selection happens here, before the worker writes the attempt
        # row, so a crash still leaves a record of which account was used.
        # The id comes from the (credential, account) pair -- each PostPeer
        # account holds its own integration id for the same TikTok account.
        pool = CredentialPool(self.conn, self.secrets, publisher=account.publisher)
        credential = pool.select_for_account(account.id, account.name)
        if not credential.resolved_id:
            raise UnknownPublisher(
                f"credential {credential.label} has no id for {account.name}"
            )
        return PostPeerTikTok(
            pool.api_key(credential),
            str(credential.resolved_id),
            credential_id=credential.id,
            draft=self.draft,
            pool=pool,
            credential=credential,
        )

    def for_credential_row(self, row) -> InstagramGraph:
        """The Instagram publisher for one credential row, for token refresh.

        Refresh is per credential, not per account: the token is the thing that
        expires, and several accounts could in principle share one.
        """
        account_ids = row["account_ids"] or {}
        ig_user_id = account_ids.get("instagram")
        if not ig_user_id:
            raise UnknownPublisher(
                f"credential {row['label']} has no instagram account id"
            )
        return InstagramGraph(self.secrets.get(row["api_key_ref"]), str(ig_user_id))

    def for_credential_id(self, credential_id: int, platform: str) -> PostPeerTikTok:
        """Rebuild the publisher that posted. §8.2.1(4) -- reconciliation must
        query the same account, since another one cannot see that post."""
        row = self.conn.execute(
            "SELECT id, project_id, publisher, label, api_key_ref, account_ids,"
            " credits_left, checked_at, enabled FROM publisher_credentials"
            " WHERE id = %s",
            (credential_id,),
        ).fetchone()
        if row is None:
            raise UnknownPublisher(f"credential {credential_id} is gone")
        from .credentials import Credential

        credential = Credential(**row)
        return PostPeerTikTok(
            self.secrets.get(credential.api_key_ref),
            str(credential.account_for(platform)),
            credential_id=credential.id,
            draft=self.draft,
        )

    def _credential_row(self, account: Account, publisher: str) -> dict:
        """The credential belonging to THIS account.

        §8.2.1: the credential is a pair, and a key belongs to the account
        whose id travels with it. Taking the first row would hand a second
        Instagram account the first account's token -- which either fails, or
        posts somewhere unintended. So the account is matched on, in order:
        its platform id, then its name as the credential label.
        """
        # The declared pairing first: publisher_account_ids is a foreign key on
        # both sides, so a row here cannot be the wrong credential.
        declared = self.conn.execute(
            "SELECT c.id, c.label, c.api_key_ref, c.account_ids,"
            " m.external_id AS resolved_id"
            " FROM publisher_credentials c"
            " JOIN publisher_account_ids m ON m.credential_id = c.id"
            " WHERE m.account_id = %s AND c.publisher = %s AND c.enabled"
            " ORDER BY c.id LIMIT 1",
            (account.id, publisher),
        ).fetchone()
        if declared is not None:
            return declared

        # Everything below is the fallback for rows registered before the join
        # table existed. New registrations always declare the pairing.
        rows = self.conn.execute(
            "SELECT id, label, api_key_ref, account_ids FROM publisher_credentials"
            " WHERE project_id = %s AND publisher = %s AND enabled ORDER BY id",
            (account.project_id, publisher),
        ).fetchall()
        if not rows:
            raise UnknownPublisher(
                f"no {publisher} credential for project {account.project_id}"
            )

        if account.external_id:
            for row in rows:
                stored = (row["account_ids"] or {}).get(account.platform)
                if stored and str(stored) == str(account.external_id):
                    return row

        for row in rows:
            if row["label"] == account.name:
                return row

        # NO "only one credential, so it must be the right one" fallback.
        # That reasoning published a post to @deployedunsafe from an unrelated
        # account on 2026-09-22: one credential does not make the pairing
        # unambiguous, it only makes the wrong answer easy to reach. An
        # unpaired account is a configuration error, and the safe response to
        # a configuration error is to refuse, never to pick the nearest token.
        raise UnknownPublisher(
            f"account {account.name!r} ({account.platform}) has no credential "
            f"paired to it; {len(rows)} credential(s) exist for this project "
            f"but none declares this account. Register it with "
            f"scripts/register_account.py, which writes the pairing."
        )
