"""Media pruning. §9.

The rule is deliberately conservative: deleting an object a target might still
need turns a retry into a permanent failure, and the URL is fetched by the
platform, not by us.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from socialq.prune import key_for, prune, usage

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)


class FakeStore:
    def __init__(self, fail_on=()):
        self.config = type("cfg", (), {"bucket": "socialq-media"})()
        self.deleted: list[str] = []
        self.fail_on = set(fail_on)
        store = self

        class Client:
            def delete_object(self, Bucket, Key):  # noqa: N803
                if Key in store.fail_on:
                    raise RuntimeError("R2 said no")
                store.deleted.append(Key)

        self.client = Client()


def seed_media(conn, *, states, created_at=OLD, bytes_=1_000_000, attached=True):
    """One media row plus a post with one target per state in `states`."""
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    media = conn.execute(
        "INSERT INTO media (project_id, sha256, url, mime, bytes, created_at)"
        " VALUES ('deploysafe', md5(random()::text),"
        " 'https://media.socialq.me/media/abc.mp4', 'video/mp4', %s, %s)"
        " RETURNING id",
        (bytes_, created_at),
    ).fetchone()["id"]
    if not attached:
        conn.commit()
        return media

    post = conn.execute(
        "INSERT INTO posts (project_id, external_id, pipeline, caption, media_ids,"
        " scheduled_for) VALUES ('deploysafe', md5(random()::text), 'brainrot',"
        " %s, %s, %s) RETURNING id",
        (json.dumps({"default": "hi"}), [media], created_at),
    ).fetchone()["id"]
    for state in states:
        account = conn.execute(
            "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
            " VALUES ('deploysafe', md5(random()::text), 'instagram', '@a',"
            " 'instagram_graph') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO targets (post_id, account_id, state) VALUES (%s,%s,%s)",
            (post, account, state),
        )
    conn.commit()
    return media


def pruned_at(conn, media_id):
    return conn.execute(
        "SELECT pruned_at FROM media WHERE id = %s", (media_id,)
    ).fetchone()["pruned_at"]


def test_the_key_is_everything_after_the_domain():
    assert key_for("https://media.socialq.me/media/abc.mp4") == "media/abc.mp4"


def test_media_for_a_published_post_is_pruned(conn):
    media = seed_media(conn, states=["published"])
    store = FakeStore()

    stats = prune(conn, store, now=lambda: NOW)

    assert (stats.pruned, store.deleted) == (1, ["media/abc.mp4"])
    assert stats.bytes_freed == 1_000_000
    assert pruned_at(conn, media) is not None


def test_media_with_a_pending_target_is_never_pruned(conn):
    """The platform may still fetch that URL."""
    media = seed_media(conn, states=["published", "pending"])
    store = FakeStore()

    assert prune(conn, store, now=lambda: NOW).pruned == 0
    assert store.deleted == []
    assert pruned_at(conn, media) is None


def test_media_with_an_in_flight_target_is_never_pruned(conn):
    seed_media(conn, states=["in_flight"])
    assert prune(conn, FakeStore(), now=lambda: NOW).pruned == 0


def test_a_dead_target_does_not_hold_media_hostage(conn):
    """A dead target will not be retried, so nothing will fetch the URL again."""
    seed_media(conn, states=["published", "dead"])
    assert prune(conn, FakeStore(), now=lambda: NOW).pruned == 1


def test_recent_media_is_kept_even_when_published(conn):
    """Reconciliation and a human debugging a bad post both need the window."""
    seed_media(conn, states=["published"], created_at=NOW - timedelta(days=2))
    assert prune(conn, FakeStore(), now=lambda: NOW).pruned == 0


# -- orphans: media registered before anything referenced it ---------------
#
# Producers register media at render time, long before publishing, so media
# with no post is now the normal case rather than a suspected bug. It gets its
# own, much longer window: an orphan is usually a video nobody has chosen yet.


def test_an_orphan_inside_the_grace_period_survives(conn):
    media = seed_media(conn, states=[], attached=False,
                       created_at=NOW - timedelta(days=10))
    assert prune(conn, FakeStore(), now=lambda: NOW).pruned == 0
    assert pruned_at(conn, media) is None


def test_an_orphan_past_the_grace_period_is_pruned(conn):
    """Without this the bucket fills with renders nobody published: ~22 of
    every 30 concepts, at ~60MB each, against a 10GB tier."""
    media = seed_media(conn, states=[], attached=False,
                       created_at=NOW - timedelta(days=40))
    store = FakeStore()

    stats = prune(conn, store, now=lambda: NOW)

    assert (stats.pruned, stats.orphans) == (1, 1)
    assert store.deleted == ["media/abc.mp4"]
    assert pruned_at(conn, media) is not None


def test_the_orphan_window_is_separate_from_the_published_one(conn):
    """They answer different questions, so one must not silently follow the
    other: 20 days is past KEEP_FOR but well inside the orphan grace."""
    media = seed_media(conn, states=[], attached=False,
                       created_at=NOW - timedelta(days=20))

    assert prune(conn, FakeStore(), now=lambda: NOW).pruned == 0
    assert prune(conn, FakeStore(), now=lambda: NOW,
                 keep_orphans_for=timedelta(days=14)).pruned == 1
    assert pruned_at(conn, media) is not None


def test_an_orphan_that_gains_a_pending_post_is_no_longer_an_orphan(conn):
    """The window a post brings is the one that applies -- a pending target
    protects its media whatever the media's age."""
    media = seed_media(conn, states=["pending"],
                       created_at=NOW - timedelta(days=90))
    assert prune(conn, FakeStore(), now=lambda: NOW).pruned == 0
    assert pruned_at(conn, media) is None


def test_already_pruned_media_is_not_deleted_twice(conn):
    seed_media(conn, states=["published"])
    store = FakeStore()
    prune(conn, store, now=lambda: NOW)
    prune(conn, store, now=lambda: NOW)
    assert len(store.deleted) == 1


def test_a_failed_delete_leaves_the_row_unpruned_for_next_time(conn):
    media = seed_media(conn, states=["published"])
    store = FakeStore(fail_on=["media/abc.mp4"])

    stats = prune(conn, store, now=lambda: NOW)

    assert (stats.pruned, stats.errors and True) == (0, True)
    assert pruned_at(conn, media) is None


def test_dry_run_deletes_nothing(conn):
    media = seed_media(conn, states=["published"])
    store = FakeStore()

    stats = prune(conn, store, now=lambda: NOW, dry_run=True)

    assert (stats.pruned, store.deleted) == (1, [])
    assert pruned_at(conn, media) is None


def test_the_biggest_objects_go_first(conn):
    seed_media(conn, states=["published"], bytes_=1_000)
    seed_media(conn, states=["published"], bytes_=9_000_000)
    store = FakeStore()

    stats = prune(conn, store, now=lambda: NOW, limit=1)

    assert (stats.pruned, stats.bytes_freed) == (1, 9_000_000)


def test_usage_counts_only_what_still_occupies_space(conn):
    seed_media(conn, states=["published"], bytes_=5_000_000)
    seed_media(conn, states=["pending"], bytes_=3_000_000)

    assert usage(conn) == {"objects": 2, "bytes": 8_000_000}

    prune(conn, FakeStore(), now=lambda: NOW)
    assert usage(conn) == {"objects": 1, "bytes": 3_000_000}


def test_usage_returns_ints_not_decimals(conn):
    """sum() over bigint is numeric, which psycopg returns as Decimal, and
    Decimal / float raises. The prune job divides by 1e9 to report GB."""
    seed_media(conn, states=["pending"], bytes_=5_000_000)
    stats = usage(conn)

    assert isinstance(stats["bytes"], int)
    assert isinstance(stats["objects"], int)
    assert stats["bytes"] / 1e9 > 0
