"""InstagramGraph. §8.3.

Exchanges are replayed through httpx's MockTransport rather than mocking the
publisher's own methods, so the request Instagram would actually receive is
what gets asserted on. §13: errors arrive in a 200 body, so every test that
cares about failure returns 200.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from socialq.media import Media
from socialq.models import Account, Post, Surface, Target
from socialq.publishers import AuthError, PublishError, RateLimitedError
from socialq.publishers.instagram import InstagramGraph

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def video(id=1, url="https://media.socialq.me/media/abc.mp4"):
    return Media(id=id, project_id="deploysafe", sha256="abc", url=url,
                 mime="video/mp4", bytes=100)


def image(id=2, mime="image/jpeg", url="https://media.socialq.me/media/abc.jpg"):
    return Media(id=id, project_id="deploysafe", sha256="abc", url=url,
                 mime=mime, bytes=100)


def make_post(**kw):
    defaults = dict(
        id=1, project_id="deploysafe", external_id="api-no-auth",
        pipeline="brainrot", caption={"default": "your frontend enforces the login."},
        scheduled_for=NOW, cta={"instagram": "link in bio"},
        hashtags={"instagram": ["#vibecoding"]}, media_ids=[1], aigc=True,
    )
    return Post(**{**defaults, **kw})


def make_target(surface=Surface.POST):
    return Target(id=7, post_id=1, account_id=3, surface=surface)


class Recorder:
    """Serves canned responses in order and keeps every request for assertions."""

    def __init__(self, responses: list[dict | tuple[int, dict]]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else {}
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx.Response(status, json=body)

    def params(self, index: int) -> dict:
        return dict(httpx.QueryParams(self.requests[index].url.query.decode()))

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def publisher(recorder: Recorder) -> InstagramGraph:
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return InstagramGraph("tok", "17841400000000000", client=client,
                          poll_interval=0, poll_timeout=1)


# -- the happy path ------------------------------------------------------


def test_single_video_is_a_reel_container_polled_then_published():
    rec = Recorder([
        {"id": "CONTAINER1"},                    # POST /media
        {"status_code": "IN_PROGRESS"},          # GET container
        {"status_code": "FINISHED"},             # GET container
        {"id": "MEDIA1"},                        # POST /media_publish
        {"permalink": "https://instagram.com/p/x"},
    ])
    result = publisher(rec).publish(make_target(), make_post(), [video()])

    assert result.provider_post_id == "MEDIA1"
    assert result.permalink == "https://instagram.com/p/x"

    create = rec.params(0)
    assert create["video_url"] == "https://media.socialq.me/media/abc.mp4"
    assert create["media_type"] == "REELS"
    assert create["caption"] == (
        "your frontend enforces the login.\n\nlink in bio\n\n#vibecoding"
    )
    assert "/media_publish" in rec.paths[3]
    assert rec.params(3)["creation_id"] == "CONTAINER1"


def test_aigc_is_disclosed_on_the_container():
    rec = Recorder([
        {"id": "C1"}, {"status_code": "FINISHED"}, {"id": "M1"}, {"permalink": "p"},
    ])
    publisher(rec).publish(make_target(), make_post(aigc=True), [video()])
    assert rec.params(0)["is_ai_generated"] == "true"


def test_non_aigc_omits_the_disclosure():
    rec = Recorder([
        {"id": "C1"}, {"status_code": "FINISHED"}, {"id": "M1"}, {"permalink": "p"},
    ])
    publisher(rec).publish(make_target(), make_post(aigc=False), [video()])
    assert "is_ai_generated" not in rec.params(0)


def test_a_story_carries_no_text_and_uses_the_stories_type():
    rec = Recorder([
        {"id": "C1"}, {"status_code": "FINISHED"}, {"id": "M1"}, {"permalink": "p"},
    ])
    publisher(rec).publish(make_target(Surface.STORY), make_post(), [image()])

    create = rec.params(0)
    assert create["media_type"] == "STORIES"
    assert "caption" not in create


def test_carousel_children_are_built_then_referenced_by_the_parent():
    rec = Recorder([
        {"id": "CH1"}, {"id": "CH2"},                 # two children
        {"status_code": "FINISHED"}, {"status_code": "FINISHED"},
        {"id": "PARENT"},                             # parent container
        {"status_code": "FINISHED"},
        {"id": "M1"}, {"permalink": "p"},
    ])
    publisher(rec).publish(make_target(), make_post(), [image(2), image(3)])

    assert rec.params(0)["is_carousel_item"] == "true"
    assert "caption" not in rec.params(0)          # caption belongs to the parent
    parent = rec.params(4)
    assert parent["media_type"] == "CAROUSEL"
    assert parent["children"] == "CH1,CH2"
    assert parent["caption"].startswith("your frontend")


def test_a_carousel_video_child_is_VIDEO_not_REELS():
    rec = Recorder([
        {"id": "CH1"}, {"id": "CH2"},
        {"status_code": "FINISHED"}, {"status_code": "FINISHED"},
        {"id": "PARENT"}, {"status_code": "FINISHED"},
        {"id": "M1"}, {"permalink": "p"},
    ])
    publisher(rec).publish(make_target(), make_post(), [video(1), image(2)])
    assert rec.params(0)["media_type"] == "VIDEO"


# -- validation, before anything is uploaded -----------------------------


def test_png_is_refused_because_instagram_takes_jpeg_only():
    rec = Recorder([])
    with pytest.raises(PublishError, match="JPEG"):
        publisher(rec).publish(make_target(), make_post(), [image(mime="image/png")])
    assert rec.requests == []


def test_more_than_ten_carousel_items_is_refused():
    rec = Recorder([])
    with pytest.raises(PublishError, match="carousel limit"):
        publisher(rec).publish(make_target(), make_post(), [image(i) for i in range(11)])
    assert rec.requests == []


def test_an_over_long_caption_is_refused():
    rec = Recorder([])
    post = make_post(caption={"default": "x" * 2300}, cta={}, hashtags={})
    with pytest.raises(PublishError, match="2300 chars"):
        publisher(rec).publish(make_target(), post, [video()])
    assert rec.requests == []


def test_too_many_hashtags_is_refused():
    rec = Recorder([])
    post = make_post(hashtags={"instagram": [f"#tag{i}" for i in range(31)]})
    with pytest.raises(PublishError, match="hashtags"):
        publisher(rec).publish(make_target(), post, [video()])
    assert rec.requests == []


# -- failures, all arriving in a 200 body --------------------------------


def test_an_expired_token_is_an_auth_error_not_a_retry():
    rec = Recorder([{"error": {"code": 190, "message": "Session has expired"}}])
    with pytest.raises(AuthError) as exc:
        publisher(rec).publish(make_target(), make_post(), [video()])
    assert exc.value.retryable is False


def test_a_rate_limit_is_classified_as_such():
    rec = Recorder([{"error": {"code": 4, "message": "Application request limit"}}])
    with pytest.raises(RateLimitedError):
        publisher(rec).publish(make_target(), make_post(), [video()])


def test_a_container_that_errors_does_not_get_published():
    rec = Recorder([
        {"id": "C1"},
        {"status_code": "ERROR", "status": "media could not be fetched"},
    ])
    with pytest.raises(PublishError, match="could not be fetched"):
        publisher(rec).publish(make_target(), make_post(), [video()])
    assert not any("media_publish" in p for p in rec.paths)


def test_a_container_stuck_in_progress_times_out_as_retryable():
    rec = Recorder([{"id": "C1"}] + [{"status_code": "IN_PROGRESS"}] * 20)
    client = httpx.Client(transport=httpx.MockTransport(rec))
    pub = InstagramGraph("tok", "ig1", client=client, poll_interval=0, poll_timeout=0)
    with pytest.raises(PublishError) as exc:
        pub.publish(make_target(), make_post(), [video()])
    assert exc.value.retryable is True


def test_losing_the_permalink_does_not_fail_a_successful_publish():
    rec = Recorder([
        {"id": "C1"}, {"status_code": "FINISHED"}, {"id": "M1"},
        {"error": {"code": 100, "message": "nope"}},   # permalink read fails
    ])
    result = publisher(rec).publish(make_target(), make_post(), [video()])
    assert result.provider_post_id == "M1"
    assert result.permalink is None


# -- reconciliation ------------------------------------------------------


def test_find_recent_pushes_since_to_the_server_and_filters_the_rest():
    since = NOW - timedelta(minutes=30)
    rec = Recorder([{"data": [
        {"id": "M1", "timestamp": "2026-09-20T11:45:00+0000",
         "permalink": "https://instagram.com/p/1",
         "media_url": "https://media.socialq.me/media/abc.mp4"},
        {"id": "M0", "timestamp": "2026-09-19T09:00:00+0000",
         "permalink": "https://instagram.com/p/0", "media_url": "old"},
    ]}])
    account = Account(id=3, project_id="deploysafe", name="n",
                      platform="instagram", handle="@n",
                      publisher="instagram_graph")

    found = publisher(rec).find_recent(account, since)

    assert [p.provider_post_id for p in found] == ["M1"]
    assert found[0].media_url == "https://media.socialq.me/media/abc.mp4"
    assert rec.params(0)["since"] == str(int(since.timestamp()))


def test_publishing_limit_reads_the_quota_off_the_account():
    rec = Recorder([{"data": [{"quota_usage": 7, "config": {"quota_total": 50}}]}])
    assert publisher(rec).publishing_limit() == (7, 50)
