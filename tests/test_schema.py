"""The schema constraints that other layers rely on being enforced by the db."""

from __future__ import annotations

import json

import psycopg
import pytest

from socialq.migrate import migrate, pending


def _project(conn, pid="deploysafe"):
    conn.execute("INSERT INTO projects (id) VALUES (%s)", (pid,))
    return pid


def _account(conn, project, platform="instagram", name="deployedunsafe"):
    row = conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (project, name, platform, f"@{name}", "instagram_graph"),
    ).fetchone()
    return row["id"]


def _post(conn, project, external_id="api-no-auth", pipeline="brainrot"):
    row = conn.execute(
        "INSERT INTO posts (project_id, external_id, pipeline, caption,"
        " media_ids, scheduled_for) VALUES (%s, %s, %s, %s, %s, now())"
        " RETURNING id",
        (project, external_id, pipeline, json.dumps({"default": "hi"}), []),
    ).fetchone()
    return row["id"]


def test_migrations_are_idempotent(database_url):
    assert migrate(database_url) == []
    with psycopg.connect(database_url) as conn:
        assert pending(conn) == []


def test_enqueue_is_idempotent_on_project_pipeline_external_id(conn):
    project = _project(conn)
    _post(conn, project)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _post(conn, project)


def test_same_external_id_from_a_different_pipeline_is_a_different_post(conn):
    project = _project(conn)
    _post(conn, project, pipeline="brainrot")
    _post(conn, project, pipeline="meme")


def test_media_is_content_addressed_per_project(conn):
    project = _project(conn)
    args = (project, "abc123", "https://media.example/abc123.mp4", "video/mp4", 10)
    sql = (
        "INSERT INTO media (project_id, sha256, url, mime, bytes)"
        " VALUES (%s, %s, %s, %s, %s)"
    )
    conn.execute(sql, args)
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(sql, args)


def test_post_and_story_are_separate_targets_for_one_account(conn):
    project = _project(conn)
    post = _post(conn, project)
    account = _account(conn, project)
    sql = (
        "INSERT INTO targets (post_id, account_id, surface) VALUES (%s, %s, %s)"
    )
    conn.execute(sql, (post, account, "post"))
    conn.execute(sql, (post, account, "story"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(sql, (post, account, "post"))


def test_attempt_idem_key_is_globally_unique(conn):
    project = _project(conn)
    post = _post(conn, project)
    account = _account(conn, project)
    target = conn.execute(
        "INSERT INTO targets (post_id, account_id) VALUES (%s, %s) RETURNING id",
        (post, account),
    ).fetchone()["id"]
    sql = "INSERT INTO attempts (target_id, n, idem_key) VALUES (%s, %s, %s)"
    conn.execute(sql, (target, 1, f"target:{target}:1"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(sql, (target, 1, f"target:{target}:1"))


def test_targets_default_to_pending(conn):
    project = _project(conn)
    post = _post(conn, project)
    account = _account(conn, project)
    row = conn.execute(
        "INSERT INTO targets (post_id, account_id) VALUES (%s, %s)"
        " RETURNING state, attempts",
        (post, account),
    ).fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 0
