"""Media layer. §9 — content addressing is what stops re-uploads."""

from __future__ import annotations

import pytest

from socialq.config import ConfigError, R2Config
from socialq.media import (
    MediaStore,
    UnsupportedMediaError,
    ensure_media,
    mime_for,
    object_key,
    sha256_file,
)

CONFIG = R2Config(
    account_id="acct",
    access_key_id="key",
    secret_access_key="secret",
    bucket="socialq",
    public_base_url="https://media.deploysafe.dev",
)


class FakeS3:
    """Records puts so a test can assert an upload did not happen twice."""

    def __init__(self, existing: set[str] | None = None):
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        for key in existing or set():
            self.objects[key] = b""

    def head_object(self, Bucket, Key):  # noqa: N803 — boto3's casing
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        self.objects[Key] = Body.read()
        self.puts.append(Key)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "api-no-auth.mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def project(conn):
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')")
    return "deploysafe"


def test_key_is_the_digest_plus_the_extension(video):
    digest = sha256_file(video)
    assert object_key(digest, video) == f"media/{digest}.mp4"


def test_unknown_extension_is_rejected(tmp_path):
    path = tmp_path / "thing.psd"
    path.write_bytes(b"x")
    with pytest.raises(UnsupportedMediaError):
        mime_for(path)


def test_r2_dev_public_url_is_refused(monkeypatch):
    for name, value in {
        "R2_ACCOUNT_ID": "a",
        "R2_ACCESS_KEY_ID": "b",
        "R2_SECRET_ACCESS_KEY": "c",
        "R2_BUCKET": "d",
        "R2_PUBLIC_BASE_URL": "https://pub-123.r2.dev",
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match="custom domain"):
        R2Config.from_env()


def test_first_enqueue_uploads_and_records_the_row(conn, project, video):
    s3 = FakeS3()
    media = ensure_media(conn, project, video, MediaStore(CONFIG, s3))

    assert media.mime == "video/mp4"
    assert media.bytes == video.stat().st_size
    assert media.url == f"https://media.deploysafe.dev/media/{media.sha256}.mp4"
    assert s3.puts == [f"media/{media.sha256}.mp4"]


def test_re_enqueueing_the_same_file_never_re_uploads(conn, project, video):
    s3 = FakeS3()
    store = MediaStore(CONFIG, s3)
    first = ensure_media(conn, project, video, store)
    second = ensure_media(conn, project, video, store)

    assert first.id == second.id
    assert len(s3.puts) == 1


def test_a_file_already_in_the_bucket_is_not_put_again(conn, project, video):
    digest = sha256_file(video)
    s3 = FakeS3(existing={f"media/{digest}.mp4"})

    media = ensure_media(conn, project, video, MediaStore(CONFIG, s3))

    assert s3.puts == []
    assert media.sha256 == digest


def test_identical_content_under_two_names_is_one_object(conn, project, video, tmp_path):
    copy = tmp_path / "renamed.mp4"
    copy.write_bytes(video.read_bytes())
    store = MediaStore(CONFIG, FakeS3())

    assert ensure_media(conn, project, video, store).id == ensure_media(
        conn, project, copy, store
    ).id


def test_a_missing_file_fails_before_any_network_call(conn, project, tmp_path):
    s3 = FakeS3()
    with pytest.raises(FileNotFoundError):
        ensure_media(conn, project, tmp_path / "nope.mp4", MediaStore(CONFIG, s3))
    assert s3.puts == []


# -- upload(): the public media API ---------------------------------------


def test_upload_returns_a_url_and_registers_the_row(conn, project, video):
    from socialq.media import upload

    s3 = FakeS3()
    url = upload(video, project=project, conn=conn, store=MediaStore(CONFIG, s3))

    digest = sha256_file(video)
    assert url == f"https://media.deploysafe.dev/media/{digest}.mp4"
    row = conn.execute(
        "SELECT sha256, url FROM media WHERE project_id = %s", (project,)
    ).fetchone()
    assert (row["sha256"], row["url"]) == (digest, url)


def test_uploading_twice_puts_once_and_makes_one_row(conn, project, video):
    from socialq.media import upload

    s3 = FakeS3()
    store = MediaStore(CONFIG, s3)
    first = upload(video, project=project, conn=conn, store=store)
    second = upload(video, project=project, conn=conn, store=store)

    assert first == second
    assert len(s3.puts) == 1
    assert conn.execute(
        "SELECT count(*) AS n FROM media"
    ).fetchone()["n"] == 1


def test_the_same_bytes_under_another_name_reuse_the_key(conn, project, video,
                                                         tmp_path):
    """The key is the content hash, so a rename is not a new object."""
    from socialq.media import upload

    renamed = tmp_path / "different-name.mp4"
    renamed.write_bytes(video.read_bytes())
    s3 = FakeS3()
    store = MediaStore(CONFIG, s3)

    assert upload(video, project=project, conn=conn, store=store) == upload(
        renamed, project=project, conn=conn, store=store)
    assert len(s3.puts) == 1


def test_the_key_scheme_is_a_contract_with_producers(video):
    """Producers upload under exactly this key. Changing the scheme is a
    breaking change for them, not an internal detail."""
    digest = sha256_file(video)
    assert object_key(digest, video) == f"media/{digest}.mp4"
    assert len(digest) == 64


# -- resolving a URL back to its row --------------------------------------


def test_a_registered_url_resolves_without_touching_the_file(conn, project, video):
    from socialq.media import media_for_url, upload

    url = upload(video, project=project, conn=conn,
                 store=MediaStore(CONFIG, FakeS3()))
    video.unlink()                      # the bytes are gone from this machine

    found = media_for_url(conn, project, url)
    assert found is not None
    assert found.url == url


def test_an_unregistered_url_resolves_to_nothing(conn, project):
    from socialq.media import media_for_url

    assert media_for_url(
        conn, project, "https://media.deploysafe.dev/media/" + "f" * 64 + ".mp4"
    ) is None


def test_a_url_from_another_project_is_not_found(conn, project, video):
    from socialq.media import media_for_url, upload

    url = upload(video, project=project, conn=conn,
                 store=MediaStore(CONFIG, FakeS3()))
    conn.execute("INSERT INTO projects (id) VALUES ('other')")

    assert media_for_url(conn, "other", url) is None


def test_a_url_that_is_not_ours_is_not_mistaken_for_one():
    from socialq.media import sha256_from_url

    assert sha256_from_url("https://example.com/video.mp4") is None
    assert sha256_from_url("https://media.deploysafe.dev/media/short.mp4") is None
    assert sha256_from_url(
        "https://media.deploysafe.dev/media/" + "a" * 64 + ".mp4"
    ) == "a" * 64
