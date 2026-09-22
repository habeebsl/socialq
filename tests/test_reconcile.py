"""Reconciliation. §7.

§13 calls the crash-recovery case the test that matters most: kill between §6
step 2 and step 3, then assert reconciliation finds the post rather than
double-posting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialq import repo
from socialq.models import RemotePost
from socialq.publishers.base import AuthError, PublishError
from socialq.reconcile import (
    Reconciler,
    due_for_refresh,
    find_match,
    refresh_tokens,
)
from socialq.secrets import EnvSecretStore, FileSecretStore, MissingSecret
from socialq.worker import MAX_ATTEMPTS, Worker

from helpers import FakePublisher, attempt_rows, seed, target_row

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(hours=2)
MEDIA_URL = "https://media.socialq.me/a.mp4"


class Crashes(FakePublisher):
    """Posts successfully, then the worker dies before recording it."""

    def publish(self, target, post, media):
        self.calls += 1
        raise SystemExit("worker killed mid-publish")


class Remote(FakePublisher):
    def __init__(self, posts):
        super().__init__()
        self.posts = posts
        self.asked_since = None

    def find_recent(self, account, since):
        self.asked_since = since
        return self.posts


def reconciler(conn, publisher, **kw):
    return Reconciler(conn, lambda a: publisher, now=lambda: NOW, **kw)


def crash_mid_publish(conn, target_id):
    """Leave the row exactly as a killed worker would: in_flight, one attempt,
    claimed long enough ago to be stale."""
    w = Worker(conn, lambda a: Crashes(), worker_id="dead", now=lambda: NOW)
    with pytest.raises(SystemExit):
        w.publish_one(repo.claim(conn, "dead", 10)[0], _Stats())
    conn.execute(
        "UPDATE targets SET claimed_at = %s WHERE id = %s", (LONG_AGO, target_id)
    )
    conn.execute(
        "UPDATE attempts SET started_at = %s WHERE target_id = %s",
        (LONG_AGO, target_id),
    )
    conn.commit()


class _Stats:
    published = retried = waiting = dead = 0
    errors: list = []


# -- the test that matters most ------------------------------------------


def test_a_post_that_landed_before_the_crash_is_recovered_not_reposted(conn):
    """§13. The publish succeeded; only the recording did not."""
    target = seed(conn)
    crash_mid_publish(conn, target)
    assert target_row(conn, target)["state"] == "in_flight"

    publisher = Remote([
        RemotePost("M1", created_at=NOW - timedelta(hours=1),
                   permalink="https://instagram.com/p/1", media_url=MEDIA_URL)
    ])
    stats = reconciler(conn, publisher).run_once()

    row = target_row(conn, target)
    assert (stats.recovered, stats.requeued) == (1, 0)
    assert row["state"] == "published"
    assert row["provider_post_id"] == "M1"
    assert row["permalink"] == "https://instagram.com/p/1"
    # No second publish was attempted.
    assert publisher.calls == 0


def test_a_post_that_never_landed_is_requeued(conn):
    target = seed(conn)
    crash_mid_publish(conn, target)

    stats = reconciler(conn, Remote([])).run_once()

    row = target_row(conn, target)
    assert (stats.recovered, stats.requeued) == (0, 1)
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > NOW


def test_recovery_closes_the_attempt_row_rather_than_leaving_it_open(conn):
    target = seed(conn)
    crash_mid_publish(conn, target)
    reconciler(conn, Remote([RemotePost("M1", media_url=MEDIA_URL)])).run_once()

    attempt = attempt_rows(conn, target)[0]
    assert attempt["finished_at"] is not None
    assert attempt["response"] == {"ok": True, "reconciled": True, "id": "M1"}


def test_a_fresh_in_flight_target_is_left_alone(conn):
    """A publish still in progress must not be mistaken for a dead worker."""
    target = seed(conn)
    crash_mid_publish(conn, target)
    conn.execute("UPDATE targets SET claimed_at = now() WHERE id = %s", (target,))
    conn.commit()

    stats = reconciler(conn, Remote([])).run_once()

    assert stats.checked == 0
    assert target_row(conn, target)["state"] == "in_flight"


def test_an_unreachable_platform_leaves_the_target_in_flight(conn):
    """Cannot tell either way: guessing here is the double-post this exists
    to prevent."""
    target = seed(conn)
    crash_mid_publish(conn, target)

    class Unreachable(FakePublisher):
        def find_recent(self, account, since):
            raise PublishError("instagram is down")

    stats = reconciler(conn, Unreachable()).run_once()

    assert target_row(conn, target)["state"] == "in_flight"
    assert stats.errors and (stats.recovered, stats.requeued) == (0, 0)


def test_an_auth_failure_during_reconcile_alerts(conn):
    target = seed(conn)
    crash_mid_publish(conn, target)
    alerts = []

    class NoAuth(FakePublisher):
        def find_recent(self, account, since):
            raise AuthError("token expired")

    r = reconciler(conn, NoAuth())
    r.alert = alerts.append
    r.run_once()

    assert alerts and "token expired" in alerts[0]
    assert target_row(conn, target)["state"] == "in_flight"


def test_a_target_requeued_past_the_cap_dies(conn):
    target = seed(conn, attempts=MAX_ATTEMPTS - 1)
    crash_mid_publish(conn, target)

    stats = reconciler(conn, Remote([])).run_once()

    assert (stats.dead, target_row(conn, target)["state"]) == (1, "dead")


def test_the_platform_is_only_asked_about_the_attempt_window(conn):
    target = seed(conn)
    crash_mid_publish(conn, target)
    publisher = Remote([])

    reconciler(conn, publisher).run_once()

    # Bounded by when the attempt started, minus slack -- not "recent posts".
    assert publisher.asked_since < LONG_AGO
    assert publisher.asked_since > LONG_AGO - timedelta(hours=1)


def test_a_stale_claimed_target_is_released_without_asking_the_platform(conn):
    target = seed(conn)
    repo.claim(conn, "dead", 10)
    conn.execute(
        "UPDATE targets SET claimed_at = %s WHERE id = %s", (LONG_AGO, target)
    )
    conn.commit()

    publisher = Remote([])
    stats = reconciler(conn, publisher).run_once()

    assert (stats.released, stats.checked) == (1, 0)
    assert target_row(conn, target)["state"] == "pending"
    assert publisher.asked_since is None


# -- matching ------------------------------------------------------------


SINCE = NOW - timedelta(hours=3)


def test_match_on_the_media_url_postpeer_style():
    remote = [RemotePost("P1", created_at=NOW, media_url=MEDIA_URL)]
    assert find_match(remote, {MEDIA_URL}, "caption", SINCE).provider_post_id == "P1"


def test_match_on_the_caption_instagram_style():
    """Instagram re-hosts media on its own CDN, so the URL never matches."""
    remote = [RemotePost("M1", created_at=NOW,
                         media_url="https://scontent.cdninstagram.com/v/xyz.mp4",
                         caption="hi")]
    assert find_match(remote, {MEDIA_URL}, "hi", SINCE).provider_post_id == "M1"


def test_a_different_caption_is_not_a_match():
    remote = [RemotePost("M1", created_at=NOW, caption="some other post")]
    assert find_match(remote, {MEDIA_URL}, "hi", SINCE) is None


def test_a_post_from_before_the_window_is_not_a_match():
    remote = [RemotePost("M1", created_at=SINCE - timedelta(days=1), caption="hi")]
    assert find_match(remote, {MEDIA_URL}, "hi", SINCE) is None


def test_an_empty_expected_caption_never_matches_on_caption():
    # A story has no text; matching "" against anything would be a wildcard.
    remote = [RemotePost("M1", created_at=NOW, caption="")]
    assert find_match(remote, {MEDIA_URL}, "", SINCE) is None


# -- token refresh -------------------------------------------------------


def test_due_for_refresh_fires_inside_the_margin_not_at_the_deadline():
    assert due_for_refresh(NOW + timedelta(days=7), NOW) is True
    assert due_for_refresh(NOW + timedelta(days=45), NOW) is False
    assert due_for_refresh(None, NOW) is True


def test_refresh_writes_the_new_token_and_its_expiry(conn, tmp_path):
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO publisher_credentials (project_id, publisher, label,"
        " api_key_ref, account_ids, expires_at) VALUES ('deploysafe',"
        " 'instagram_graph', 'main', 'IG_TOKEN', '{}', %s)",
        (NOW + timedelta(days=3),),
    )
    conn.commit()
    store = FileSecretStore(tmp_path / "secrets.json")
    store.set("IG_TOKEN", "old-token")
    new_expiry = NOW + timedelta(days=60)

    class Refreshing(FakePublisher):
        def refresh_token(self):
            return "new-token", new_expiry

    refreshed = refresh_tokens(
        conn, lambda row: (Refreshing(), store), now=lambda: NOW
    )

    assert refreshed == ["main"]
    assert store.get("IG_TOKEN") == "new-token"
    row = conn.execute(
        "SELECT expires_at, refreshed_at FROM publisher_credentials"
    ).fetchone()
    assert row["expires_at"] == new_expiry
    assert row["refreshed_at"] is not None


def test_a_failed_refresh_is_loud_and_leaves_the_old_token(conn, tmp_path, caplog):
    """§14: token expiry is the most likely production failure."""
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO publisher_credentials (project_id, publisher, label,"
        " api_key_ref, account_ids, expires_at) VALUES ('deploysafe',"
        " 'instagram_graph', 'main', 'IG_TOKEN', '{}', %s)",
        (NOW + timedelta(days=3),),
    )
    conn.commit()
    store = FileSecretStore(tmp_path / "secrets.json")
    store.set("IG_TOKEN", "old-token")

    class Failing(FakePublisher):
        def refresh_token(self):
            raise AuthError("token has expired and cannot be refreshed")

    with caplog.at_level("ERROR"):
        refreshed = refresh_tokens(
            conn, lambda row: (Failing(), store), now=lambda: NOW
        )

    assert refreshed == []
    assert store.get("IG_TOKEN") == "old-token"
    assert "ALERT" in caplog.text


def test_the_env_store_refuses_to_write_rather_than_lose_a_token(monkeypatch):
    """Silently dropping a refreshed token burns the account's 60 days."""
    monkeypatch.setenv("IG_TOKEN", "value")
    store = EnvSecretStore()
    assert store.get("IG_TOKEN") == "value"
    with pytest.raises(NotImplementedError, match="read-only"):
        store.set("IG_TOKEN", "new")


def test_a_missing_secret_is_an_error_not_an_empty_string():
    with pytest.raises(MissingSecret):
        EnvSecretStore().get("NOT_SET_ANYWHERE_12345")
