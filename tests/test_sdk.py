"""The SDK. §10.

The property that matters: enqueue is idempotent on
(project, pipeline, external_id), and a partial enqueue is impossible.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialq.media import MediaStore
from socialq.sdk import Client, ValidationError

from test_media import CONFIG, FakeS3

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "api-no-auth.mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def jpeg(tmp_path):
    path = tmp_path / "card.jpg"
    path.write_bytes(b"not really a jpeg")
    return path


@pytest.fixture
def client(database_url, tmp_path):
    return Client(database_url, store=MediaStore(CONFIG, FakeS3()),
                  base_dir=tmp_path)


@pytest.fixture
def accounts(conn):
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    for platform, publisher in (("instagram", "instagram_graph"),
                                ("tiktok", "postpeer")):
        conn.execute(
            "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
            " VALUES ('deploysafe', %s, %s, %s, %s)",
            (f"main-{platform}", platform, f"@main-{platform}", publisher),
        )
    conn.commit()


def enqueue(client, video, **kw):
    args = dict(
        project="deploysafe", pipeline="brainrot", external_id="api-no-auth",
        media=[video.name], caption={"default": "your api doesnt"},
        surface={"instagram": "post"},
        accounts={"instagram": ["main-instagram"]},
        cta={"instagram": "link in bio"},
        hashtags={"instagram": ["#vibecoding"]}, aigc=True, at=NOW,
    )
    merged = {**args, **kw}
    # Keep surface and accounts in step unless a test sets both deliberately.
    if "surface" in kw and "accounts" not in kw:
        merged["accounts"] = {p: [f"main-{p}"] for p in kw["surface"]}
    return client.enqueue(**merged)


# -- the happy path ------------------------------------------------------


def test_enqueue_writes_the_post_the_media_and_one_target_per_account(
    conn, client, video, accounts
):
    result = enqueue(client, video, surface={"instagram": "post", "tiktok": "post"})

    assert len(result.targets) == 2
    assert len(result.media_ids) == 1

    post = conn.execute("SELECT * FROM posts WHERE id = %s",
                        (result.post_id,)).fetchone()
    assert post["caption"] == {"default": "your api doesnt"}
    assert post["aigc"] is True
    assert post["scheduled_for"] == NOW

    states = conn.execute(
        "SELECT state FROM targets WHERE post_id = %s", (result.post_id,)
    ).fetchall()
    assert [row["state"] for row in states] == ["pending", "pending"]


def test_a_story_and_a_post_to_one_platform_are_separate_targets(
    conn, client, video, jpeg, accounts
):
    result = enqueue(client, video, surface={"instagram": "story"},
                     media=[jpeg.name], cta={}, hashtags={})
    assert len(result.targets) == 1
    surface = conn.execute(
        "SELECT surface FROM targets WHERE post_id = %s", (result.post_id,)
    ).fetchone()["surface"]
    assert surface == "story"


def test_media_is_uploaded_once_across_two_posts(conn, client, video, accounts):
    first = enqueue(client, video)
    second = enqueue(client, video, external_id="another")

    assert first.media_ids == second.media_ids
    assert len(client.store.client.puts) == 1


def test_a_disabled_account_still_gets_a_target(conn, client, video, accounts):
    """§14: warming means an account is registered but held back. The claim
    query is what skips it, not the enqueue."""
    conn.execute("UPDATE accounts SET enabled = false")
    conn.commit()

    result = enqueue(client, video)
    assert len(result.targets) == 1


def test_at_defaults_to_now_so_an_omitted_schedule_publishes_immediately(
    conn, client, video, accounts
):
    result = enqueue(client, video, at=None)
    scheduled = conn.execute(
        "SELECT scheduled_for FROM posts WHERE id = %s", (result.post_id,)
    ).fetchone()["scheduled_for"]
    assert scheduled <= datetime.now(timezone.utc)


# -- idempotence ---------------------------------------------------------


def test_enqueueing_the_same_sidecar_twice_makes_one_post_and_n_targets(
    conn, client, video, accounts
):
    """§13. Re-running a batch must not double-post."""
    first = enqueue(client, video, surface={"instagram": "post", "tiktok": "post"})
    second = enqueue(client, video, surface={"instagram": "post", "tiktok": "post"})

    assert second.post_id == first.post_id
    assert second.created is False
    assert conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM targets").fetchone()["n"] == 2


def test_the_second_enqueue_reports_the_targets_that_already_exist(
    conn, client, video, accounts
):
    first = enqueue(client, video)
    second = enqueue(client, video)
    assert second.targets == first.targets


def test_the_same_external_id_in_another_pipeline_is_a_different_post(
    conn, client, video, accounts
):
    first = enqueue(client, video)
    second = enqueue(client, video, pipeline="meme")
    assert second.post_id != first.post_id


# -- a partial enqueue must be impossible --------------------------------


def test_nothing_is_written_when_a_later_platform_fails_validation(
    conn, client, video, accounts
):
    """One transaction: an instagram target must not survive a tiktok error."""
    with pytest.raises(ValidationError):
        enqueue(client, video,
                surface={"instagram": "post", "tiktok": "story"})

    assert conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM targets").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"] == 0


def test_a_missing_account_writes_nothing(conn, client, video, accounts):
    with pytest.raises(ValidationError, match="no linkedin account named"):
        enqueue(client, video, surface={"linkedin": "post"},
                accounts={"linkedin": ["main-linkedin"]},
                cta={}, hashtags={}, caption={"default": "x"})
    assert conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"] == 0


def test_a_missing_media_file_writes_nothing(conn, client, video, accounts):
    with pytest.raises(FileNotFoundError):
        enqueue(client, video, media=["does-not-exist.mp4"])
    assert conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"] == 0


# -- validation in front of the caller -----------------------------------


def test_an_unknown_platform_is_refused(client, video):
    with pytest.raises(ValidationError, match="unknown platform"):
        enqueue(client, video, surface={"mastodon": "post"})


def test_an_unknown_surface_is_refused(client, video):
    with pytest.raises(ValidationError, match="post' or 'story"):
        enqueue(client, video, surface={"instagram": "reel"})


def test_a_caption_key_for_a_platform_nothing_posts_to_is_refused(client, video):
    """A silent typo otherwise: the text simply never appears."""
    with pytest.raises(ValidationError, match="not in surface"):
        enqueue(client, video, caption={"default": "x", "tiktok": "typo"},
                surface={"instagram": "post"})


def test_an_over_long_instagram_caption_is_refused_at_enqueue(
    client, video, accounts
):
    """§10: failing here beats failing in the worker six hours later."""
    with pytest.raises(Exception, match="2200"):
        enqueue(client, video, caption={"default": "x" * 2300},
                cta={}, hashtags={})


def test_a_png_is_refused_at_enqueue_because_instagram_takes_jpeg_only(
    client, tmp_path, accounts
):
    png = tmp_path / "card.png"
    png.write_bytes(b"not really a png")
    with pytest.raises(Exception, match="JPEG"):
        client.enqueue(project="deploysafe", pipeline="brainrot",
                       external_id="x", media=[png.name],
                       caption={"default": "hi"}, surface={"instagram": "post"},
                       accounts={"instagram": ["main-instagram"]})


def test_tiktok_has_no_story_surface(client, video, accounts):
    with pytest.raises(ValidationError, match="no story surface"):
        enqueue(client, video, surface={"tiktok": "story"},
                cta={}, hashtags={}, caption={"default": "x"})


def test_an_empty_surface_is_refused(client, video):
    with pytest.raises(ValidationError, match="nothing would be posted"):
        enqueue(client, video, surface={}, accounts={}, cta={}, hashtags={})


def test_an_unparseable_schedule_is_refused(client, video):
    with pytest.raises(ValidationError, match="cannot parse"):
        enqueue(client, video, at="next tuesday")


def test_an_iso_string_schedule_is_accepted(conn, client, video, accounts):
    result = enqueue(client, video, at="2026-09-22T14:00:00Z")
    scheduled = conn.execute(
        "SELECT scheduled_for FROM posts WHERE id = %s", (result.post_id,)
    ).fetchone()["scheduled_for"]
    assert scheduled == datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


# -- explicit destinations -----------------------------------------------


def test_only_the_named_accounts_get_targets(conn, client, video, accounts):
    """Two instagram accounts registered, one named: one target."""
    conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
        " VALUES ('deploysafe','second','instagram','@second','instagram_graph')"
    )
    conn.commit()

    result = enqueue(client, video, accounts={"instagram": ["main-instagram"]})

    assert len(result.targets) == 1
    name = conn.execute(
        "SELECT a.name FROM targets t JOIN accounts a ON a.id = t.account_id"
        " WHERE t.post_id = %s", (result.post_id,)
    ).fetchone()["name"]
    assert name == "main-instagram"


def test_naming_two_accounts_on_one_platform_makes_two_targets(
    conn, client, video, accounts
):
    conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
        " VALUES ('deploysafe','second','instagram','@second','instagram_graph')"
    )
    conn.commit()

    result = enqueue(client, video,
                     accounts={"instagram": ["main-instagram", "second"]})

    assert len(result.targets) == 2


def test_registering_another_account_does_not_change_where_a_post_goes(
    conn, client, video, accounts
):
    """The reason accounts is required: a new account must never silently
    redirect existing callers onto a real account."""
    first = enqueue(client, video)

    conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
        " VALUES ('deploysafe','second','instagram','@second','instagram_graph')"
    )
    conn.commit()
    second = enqueue(client, video, external_id="later")

    assert len(first.targets) == len(second.targets) == 1


def test_accounts_is_required(client, video):
    with pytest.raises(TypeError, match="accounts"):
        client.enqueue(project="deploysafe", pipeline="brainrot",
                       external_id="x", media=[video.name],
                       caption={"default": "hi"}, surface={"instagram": "post"})


def test_a_platform_in_surface_but_not_accounts_is_refused(client, video):
    with pytest.raises(ValidationError, match="accounts does not"):
        enqueue(client, video, surface={"instagram": "post", "tiktok": "post"},
                accounts={"instagram": ["main-instagram"]})


def test_a_platform_in_accounts_but_not_surface_is_refused(client, video):
    with pytest.raises(ValidationError, match="surface does not"):
        enqueue(client, video, surface={"instagram": "post"},
                accounts={"instagram": ["main-instagram"], "tiktok": ["x"]})


def test_an_empty_account_list_is_refused(client, video):
    with pytest.raises(ValidationError, match="is empty"):
        enqueue(client, video, accounts={"instagram": []})


def test_a_bare_string_instead_of_a_list_is_refused(client, video):
    """"deployedunsafe" would otherwise be read as a list of characters."""
    with pytest.raises(ValidationError, match="must be a list"):
        enqueue(client, video, accounts={"instagram": "main-instagram"})


def test_an_unregistered_account_name_lists_what_is_available(
    conn, client, video, accounts
):
    with pytest.raises(ValidationError, match="main-instagram"):
        enqueue(client, video, accounts={"instagram": ["typo"]})


# -- media by URL: register at render time, publish later -----------------


def test_enqueue_accepts_a_registered_url_with_no_local_file(
    conn, client, video, accounts
):
    """The point of the whole feature: publishing need not happen on the
    machine that rendered the file."""
    url = client.upload(video.name, project="deploysafe")
    video.unlink()

    result = enqueue(client, video, media=[url])

    assert len(result.targets) == 1
    media_id = conn.execute(
        "SELECT media_ids FROM posts WHERE id = %s", (result.post_id,)
    ).fetchone()["media_ids"]
    assert conn.execute(
        "SELECT url FROM media WHERE id = %s", (media_id[0],)
    ).fetchone()["url"] == url


def test_a_url_is_not_uploaded_again(conn, client, video, accounts):
    url = client.upload(video.name, project="deploysafe")
    puts_after_upload = len(client.store.client.puts)

    enqueue(client, video, media=[url])

    assert len(client.store.client.puts) == puts_after_upload


def test_an_unregistered_url_is_refused_and_says_what_to_do(
    conn, client, video, accounts
):
    url = "https://media.socialq.me/media/" + "b" * 64 + ".mp4"

    with pytest.raises(ValidationError, match="upload it first"):
        enqueue(client, video, media=[url])

    assert conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"] == 0


def test_an_unregistered_url_is_never_fetched(conn, client, video, accounts):
    """Downloading 60MB to verify a URL would defeat passing one."""
    import httpx

    def explode(*a, **k):
        raise AssertionError("the URL must not be fetched")

    original = httpx.get
    httpx.get = explode
    try:
        with pytest.raises(ValidationError):
            enqueue(client, video,
                    media=["https://media.socialq.me/media/" + "c" * 64 + ".mp4"])
    finally:
        httpx.get = original


def test_a_url_registered_to_another_project_is_refused(
    conn, client, video, accounts
):
    url = client.upload(video.name, project="deploysafe")
    conn.execute("INSERT INTO projects (id) VALUES ('other')")
    conn.commit()

    with pytest.raises(ValidationError, match="not registered"):
        enqueue(client, video, project="other", media=[url])


def test_paths_still_work_exactly_as_before(conn, client, video, accounts):
    """Every existing caller passes paths; base_dir resolution is unchanged."""
    result = enqueue(client, video, media=[video.name])
    assert len(result.targets) == 1


def test_upload_is_idempotent_through_the_client(conn, client, video):
    first = client.upload(video.name, project="deploysafe")
    second = client.upload(video.name, project="deploysafe")

    assert first == second
    assert len(client.store.client.puts) == 1
