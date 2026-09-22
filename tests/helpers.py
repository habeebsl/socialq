"""Fixtures shared by the worker and reconcile tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from socialq.models import Result

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def seed(conn, *, scheduled_for=None, enabled=True, attempts=0, next_attempt_at=None):
    """One project, account, media, post and target. Returns the target id."""
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    account = conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher,"
        " enabled) VALUES ('deploysafe', md5(random()::text), 'instagram','@main',"
        "'instagram_graph',%s) RETURNING id",
        (enabled,),
    ).fetchone()["id"]
    media = conn.execute(
        "INSERT INTO media (project_id, sha256, url, mime, bytes)"
        " VALUES ('deploysafe', md5(random()::text), 'https://media.socialq.me/a.mp4',"
        " 'video/mp4', 10) RETURNING id"
    ).fetchone()["id"]
    post = conn.execute(
        "INSERT INTO posts (project_id, external_id, pipeline, caption, media_ids,"
        " scheduled_for) VALUES ('deploysafe', md5(random()::text), 'brainrot',"
        " %s, %s, %s) RETURNING id",
        (json.dumps({"default": "hi"}), [media],
         scheduled_for or NOW - timedelta(minutes=1)),
    ).fetchone()["id"]
    target = conn.execute(
        "INSERT INTO targets (post_id, account_id, attempts, next_attempt_at)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (post, account, attempts, next_attempt_at),
    ).fetchone()["id"]
    conn.commit()
    return target


def target_row(conn, target_id):
    return conn.execute(
        "SELECT * FROM targets WHERE id = %s", (target_id,)
    ).fetchone()


def attempt_rows(conn, target_id):
    return conn.execute(
        "SELECT * FROM attempts WHERE target_id = %s ORDER BY n", (target_id,)
    ).fetchall()


class FakePublisher:
    """Publishes, or raises whatever the test asked for."""

    name = "fake"

    def __init__(self, result=None, error=None):
        self.result = result or Result(provider_post_id="P1", permalink="https://x/1")
        self.error = error
        self.calls = 0

    def publish(self, target, post, media):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def find_recent(self, account, since):
        return []
