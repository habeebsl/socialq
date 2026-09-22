"""PostPeer/TikTok publisher and credential rotation. §8.2, §8.2.1.

No test here spends a credit: exchanges are replayed through MockTransport.
§13 -- the API reports failures in a 200 body, so every failure test returns 200.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from socialq.credentials import CredentialPool
from socialq.media import Media
from socialq.models import Post, Target
from socialq.publishers.base import AuthError, OutOfCreditError, PublishError
from socialq.publishers.postpeer import PostPeerTikTok
from socialq.registry import Registry, UnknownPublisher
from socialq.secrets import FileSecretStore

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
VIDEO_URL = "https://media.socialq.me/media/abc.mp4"


def video():
    return Media(id=1, project_id="deploysafe", sha256="abc", url=VIDEO_URL,
                 mime="video/mp4", bytes=100)


def photo(n=1):
    return Media(id=n, project_id="deploysafe", sha256=f"p{n}",
                 url=f"https://media.socialq.me/media/p{n}.jpg",
                 mime="image/jpeg", bytes=100)


def make_post(**kw):
    defaults = dict(
        id=1, project_id="deploysafe", external_id="api-no-auth",
        pipeline="brainrot", caption={"default": "your api doesnt"},
        scheduled_for=NOW, cta={"tiktok": "link in bio"},
        hashtags={"tiktok": ["#vibecoding"]}, media_ids=[1], aigc=True,
    )
    return Post(**{**defaults, **kw})


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else {}
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx.Response(status, json=body)

    def json(self, index=0):
        import json as _json
        return _json.loads(self.requests[index].content)


def publisher(rec, **kw):
    return PostPeerTikTok("key-1", "tt_321",
                          client=httpx.Client(transport=httpx.MockTransport(rec)),
                          **kw)


OK = {"success": True, "status": "published", "postId": "post_abc123",
      "platforms": [{"platform": "tiktok", "success": True,
                     "platformPostUrl": "https://www.tiktok.com/@u/video/7123"}]}


# -- the request ---------------------------------------------------------


def test_a_video_post_matches_the_documented_shape():
    rec = Recorder([OK])
    result = publisher(rec).publish(Target(id=1, post_id=1, account_id=1),
                                    make_post(), [video()])

    body = rec.json()
    assert body["mediaItems"] == [{"type": "video", "url": VIDEO_URL}]
    assert body["publishNow"] is True
    assert body["content"] == "your api doesnt\n\nlink in bio\n\n#vibecoding"

    platform = body["platforms"][0]
    assert platform["platform"] == "tiktok"
    assert platform["accountId"] == "tt_321"
    assert platform["platformSpecificData"]["privacyLevel"] == "PUBLIC_TO_EVERYONE"
    assert platform["platformSpecificData"]["draft"] is False

    assert result.provider_post_id == "post_abc123"
    assert result.permalink == "https://www.tiktok.com/@u/video/7123"


def test_the_access_key_goes_in_the_x_access_key_header():
    rec = Recorder([OK])
    publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                           [video()])
    assert rec.requests[0].headers["x-access-key"] == "key-1"


def test_aigc_is_taken_from_the_post_not_hardcoded():
    rec = Recorder([OK, OK])
    target = Target(id=1, post_id=1, account_id=1)
    pub = publisher(rec)

    pub.publish(target, make_post(aigc=True), [video()])
    assert rec.json(0)["platforms"][0]["platformSpecificData"]["isAigc"] is True

    pub.publish(target, make_post(aigc=False), [video()])
    assert rec.json(1)["platforms"][0]["platformSpecificData"]["isAigc"] is False


def test_draft_mode_is_explicit_so_a_smoke_test_posts_nothing_public():
    rec = Recorder([OK])
    publisher(rec, draft=True).publish(Target(id=1, post_id=1, account_id=1),
                                       make_post(), [video()])
    assert rec.json()["platforms"][0]["platformSpecificData"]["draft"] is True


def test_a_photo_carousel_puts_the_title_in_content_and_the_body_in_description():
    """§8.2: content is the 90-char title; description is separate at 4,000."""
    rec = Recorder([OK])
    publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                           [photo(1), photo(2)])

    body = rec.json()
    assert body["content"] == "your api doesnt"           # caption alone
    data = body["platforms"][0]["platformSpecificData"]
    assert data["description"] == "your api doesnt\n\nlink in bio\n\n#vibecoding"
    assert [m["type"] for m in body["mediaItems"]] == ["image", "image"]


# -- validation ----------------------------------------------------------


def test_mixing_video_and_photos_is_refused():
    rec = Recorder([])
    with pytest.raises(PublishError, match="video or photos"):
        publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                               [video(), photo()])
    assert rec.requests == []


def test_an_over_long_photo_title_is_refused():
    rec = Recorder([])
    post = make_post(caption={"default": "x" * 91})
    with pytest.raises(PublishError, match="90"):
        publisher(rec).publish(Target(id=1, post_id=1, account_id=1), post, [photo()])
    assert rec.requests == []


def test_more_than_thirty_two_images_is_refused():
    rec = Recorder([])
    with pytest.raises(PublishError, match="32"):
        publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                               [photo(i) for i in range(33)])


# -- failures in a 200 body ----------------------------------------------


def test_a_failed_envelope_is_an_error_despite_http_200():
    rec = Recorder([{"success": False, "message": "something broke"}])
    with pytest.raises(PublishError, match="something broke"):
        publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                               [video()])


def test_a_failed_platform_entry_is_an_error_even_when_the_envelope_succeeds():
    rec = Recorder([{"success": True, "postId": "p1", "platforms": [
        {"platform": "tiktok", "success": False, "error": "account disconnected"}
    ]}])
    with pytest.raises(PublishError, match="account disconnected"):
        publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                               [video()])


def test_an_insufficient_credit_message_is_not_a_generic_failure():
    """§8.2.1: it means try the next credential, not retry this one."""
    rec = Recorder([(400, {"message": "Insufficient credits for this request"})])
    with pytest.raises(OutOfCreditError):
        publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                               [video()])


def test_a_revoked_key_is_an_auth_error():
    rec = Recorder([(401, {"message": "Unauthorized"})])
    with pytest.raises(AuthError):
        publisher(rec).publish(Target(id=1, post_id=1, account_id=1), make_post(),
                               [video()])


# -- reconciliation ------------------------------------------------------


def test_find_recent_matches_on_the_url_we_submitted():
    rec = Recorder([{"posts": [
        {"postId": "post_abc123", "createdAt": "2026-09-20T11:00:00Z",
         "content": "your api doesnt",
         "mediaItems": [{"type": "video", "url": VIDEO_URL}],
         "platforms": [{"platform": "tiktok",
                        "platformPostUrl": "https://tiktok.com/@u/video/7123"}]},
    ]}])
    found = publisher(rec).find_recent(None, NOW - timedelta(hours=2))

    assert [p.media_url for p in found] == [VIDEO_URL]
    assert found[0].provider_post_id == "post_abc123"
    assert found[0].permalink == "https://tiktok.com/@u/video/7123"


def test_find_recent_drops_posts_from_before_the_window():
    rec = Recorder([{"posts": [
        {"postId": "old", "createdAt": "2026-09-01T11:00:00Z",
         "mediaItems": [{"url": VIDEO_URL}]},
    ]}])
    assert publisher(rec).find_recent(None, NOW - timedelta(hours=2)) == []


def test_credits_remaining_reads_whichever_key_the_api_uses():
    for body in ({"creditsRemaining": 12}, {"credits_left": 12},
                 {"usage": {"remaining": 12}}, {"quota": 20, "usage": {"used": 8}}):
        assert publisher(Recorder([body])).credits_remaining() == 12


# -- credential rotation, §8.2.1 -----------------------------------------


def seed_credentials(conn, spec, checked_at=NOW):
    """spec: [(label, credits_left, enabled)]

    checked_at is pinned to the tests' fake clock, not the database's now():
    mixing the two makes staleness depend on the wall clock.
    """
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    for label, credits, enabled in spec:
        conn.execute(
            "INSERT INTO publisher_credentials (project_id, publisher, label,"
            " api_key_ref, account_ids, credits_left, checked_at, enabled)"
            " VALUES ('deploysafe','postpeer',%s,%s,%s,%s,%s,%s)",
            (label, f"KEY_{label}", f'{{"tiktok": "tt_{label}"}}', credits,
             checked_at, enabled),
        )
    conn.commit()


def pool(conn, tmp_path, **kw):
    store = FileSecretStore(tmp_path / "s.json")
    for label in ("a", "b", "c"):
        store.set(f"KEY_{label}", f"secret-{label}")
    return CredentialPool(conn, store, now=lambda: NOW, **kw)


def test_the_credential_with_the_most_credits_wins(conn, tmp_path):
    """Most, not first-with-any: it spreads load and leaves retry headroom."""
    seed_credentials(conn, [("a", 3, True), ("b", 14, True), ("c", 7, True)])
    assert pool(conn, tmp_path).select("deploysafe", "tiktok").label == "b"


def test_a_disabled_credential_is_never_selected(conn, tmp_path):
    seed_credentials(conn, [("a", 3, True), ("b", 99, False)])
    assert pool(conn, tmp_path).select("deploysafe", "tiktok").label == "a"


def test_exhaustion_is_a_wait_not_a_failure(conn, tmp_path):
    """§8.2.1(3): running out of credit must not fail the target."""
    seed_credentials(conn, [("a", 0, True), ("b", 0, True)])
    with pytest.raises(OutOfCreditError):
        pool(conn, tmp_path).select("deploysafe", "tiktok")


def test_the_key_and_the_account_id_travel_together(conn, tmp_path):
    """The credential is a pair: a key with another account's id posts to the
    wrong place."""
    seed_credentials(conn, [("a", 5, True), ("b", 9, True)])
    p = pool(conn, tmp_path)
    chosen = p.select("deploysafe", "tiktok")

    assert chosen.label == "b"
    assert p.api_key(chosen) == "secret-b"
    assert chosen.account_for("tiktok") == "tt_b"


def test_usage_is_not_re_read_while_the_count_is_fresh(conn, tmp_path):
    """GET /v1/usage/ costs a credit itself -- 5% of a free month."""
    seed_credentials(conn, [("a", 10, True)])
    calls = []
    p = pool(conn, tmp_path, fetch_usage=lambda c: calls.append(c) or 10)
    p.select("deploysafe", "tiktok")
    assert calls == []


def test_a_stale_count_is_refreshed_before_selecting(conn, tmp_path):
    seed_credentials(conn, [("a", 1, True), ("b", 2, True)],
                     checked_at=NOW - timedelta(hours=2))
    # 'a' has really been topped up; the cached counts would pick 'b'.
    p = pool(conn, tmp_path,
             fetch_usage=lambda c: 20 if c.label == "a" else 2)
    assert p.select("deploysafe", "tiktok").label == "a"


def test_a_failed_usage_check_does_not_stop_a_publish(conn, tmp_path):
    seed_credentials(conn, [("a", 5, True)], checked_at=None)

    def boom(credential):
        raise RuntimeError("usage endpoint down")

    assert pool(conn, tmp_path, fetch_usage=boom).select(
        "deploysafe", "tiktok"
    ).label == "a"


def test_spending_decrements_locally_rather_than_re_reading_usage(conn, tmp_path):
    seed_credentials(conn, [("a", 5, True)])
    p = pool(conn, tmp_path)
    credential = p.select("deploysafe", "tiktok")
    p.spend(credential)

    left = conn.execute(
        "SELECT credits_left FROM publisher_credentials WHERE label = 'a'"
    ).fetchone()["credits_left"]
    assert (credential.credits_left, left) == (4, 4)


def test_a_credential_reporting_no_credit_mid_batch_is_skipped_next_time(conn, tmp_path):
    """§8.2.1: a publish can still fail for credit after the check."""
    seed_credentials(conn, [("a", 9, True), ("b", 4, True)])
    p = pool(conn, tmp_path)
    first = p.select("deploysafe", "tiktok")
    assert first.label == "a"

    p.exhausted(first)

    assert p.select("deploysafe", "tiktok").label == "b"


def test_a_revoked_credential_is_disabled_and_the_others_carry_on(conn, tmp_path):
    """One dead account must never stall the queue."""
    seed_credentials(conn, [("a", 9, True), ("b", 4, True)])
    p = pool(conn, tmp_path)
    p.disable(p.select("deploysafe", "tiktok"), "revoked")

    assert p.select("deploysafe", "tiktok").label == "b"


def test_a_credential_whose_secret_is_missing_is_disabled_not_retried_forever(
    conn, tmp_path
):
    seed_credentials(conn, [("a", 9, True)])
    store = FileSecretStore(tmp_path / "empty.json")
    p = CredentialPool(conn, store, now=lambda: NOW)
    credential = p.select("deploysafe", "tiktok")

    with pytest.raises(KeyError):
        p.api_key(credential)

    enabled = conn.execute(
        "SELECT enabled FROM publisher_credentials WHERE label = 'a'"
    ).fetchone()["enabled"]
    assert enabled is False


# -- routing, §8.4 -------------------------------------------------------


def test_x_through_postpeer_is_refused_in_code(conn, tmp_path):
    """50 credits for one post with a URL: two and a half months of a free
    account. §2 says never, so it is not left to configuration."""
    from socialq.models import Account

    registry = Registry(conn, FileSecretStore(tmp_path / "s.json"))
    account = Account(id=1, project_id="deploysafe", name="x", platform="x",
                      handle="@x", publisher="postpeer")
    with pytest.raises(UnknownPublisher, match="credit cost"):
        registry(account)


def test_an_unknown_publisher_name_is_an_error(conn, tmp_path):
    from socialq.models import Account

    registry = Registry(conn, FileSecretStore(tmp_path / "s.json"))
    account = Account(id=1, project_id="deploysafe", name="n", platform="tiktok",
                      handle="@n", publisher="buffer")
    with pytest.raises(UnknownPublisher):
        registry(account)


def seed_tiktok_account(conn, name="tt"):
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    return conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
        " VALUES ('deploysafe', %s, 'tiktok', %s, 'postpeer') RETURNING id",
        (name, f"@{name}"),
    ).fetchone()["id"]


def connect_credential(conn, label, account_id, external_id):
    """One cell of the (credential x account) grid -- the id THIS PostPeer
    account uses for THAT TikTok account."""
    cred = conn.execute(
        "SELECT id FROM publisher_credentials WHERE label = %s", (label,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO publisher_account_ids (credential_id, account_id, external_id)"
        " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (cred, account_id, external_id),
    )
    conn.commit()
    return cred


def test_the_registry_fixes_the_credential_before_the_attempt_is_written(
    conn, tmp_path
):
    """§8.2.1(4): reconciliation must know which account posted, even if the
    worker dies immediately after."""
    from socialq.models import Account

    seed_credentials(conn, [("a", 9, True)])
    account_id = seed_tiktok_account(conn)
    connect_credential(conn, "a", account_id, "tt_a")

    store = FileSecretStore(tmp_path / "s.json")
    store.set("KEY_a", "secret-a")
    registry = Registry(conn, store)
    account = Account(id=account_id, project_id="deploysafe", name="tt",
                      platform="tiktok", handle="@tt", publisher="postpeer")

    built = registry(account)

    assert built.credential_id is not None
    assert built.account_id == "tt_a"
    assert built.api_key == "secret-a"


def test_the_same_tiktok_account_has_a_different_id_per_postpeer_account(
    conn, tmp_path
):
    """The grid. Each PostPeer account authorised TikTok separately, so each
    holds its own integration id for the same account."""
    from socialq.models import Account

    seed_credentials(conn, [("a", 3, True), ("b", 17, True)])
    account_id = seed_tiktok_account(conn)
    connect_credential(conn, "a", account_id, "tt_via_a")
    connect_credential(conn, "b", account_id, "tt_via_b")

    store = FileSecretStore(tmp_path / "s.json")
    for label in ("a", "b"):
        store.set(f"KEY_{label}", f"secret-{label}")
    account = Account(id=account_id, project_id="deploysafe", name="tt",
                      platform="tiktok", handle="@tt", publisher="postpeer")

    built = Registry(conn, store)(account)

    # 'b' has the most credits, so its own id for this account must be used --
    # not 'a'\'s, which would post somewhere unintended (§8.2.1).
    assert (built.api_key, built.account_id) == ("secret-b", "tt_via_b")


def test_a_credential_not_connected_to_the_account_is_not_used(conn, tmp_path):
    """A missing row means that PostPeer account cannot reach this TikTok
    account -- even though it has far more credit."""
    from socialq.models import Account

    seed_credentials(conn, [("a", 3, True), ("b", 99, True)])
    account_id = seed_tiktok_account(conn)
    connect_credential(conn, "a", account_id, "tt_via_a")   # 'b' not connected

    store = FileSecretStore(tmp_path / "s.json")
    for label in ("a", "b"):
        store.set(f"KEY_{label}", f"secret-{label}")
    account = Account(id=account_id, project_id="deploysafe", name="tt",
                      platform="tiktok", handle="@tt", publisher="postpeer")

    built = Registry(conn, store)(account)

    assert (built.api_key, built.account_id) == ("secret-a", "tt_via_a")


def test_an_account_no_credential_is_connected_to_is_refused(conn, tmp_path):
    from socialq.models import Account

    seed_credentials(conn, [("a", 9, True)])
    account_id = seed_tiktok_account(conn, "orphan")

    store = FileSecretStore(tmp_path / "s.json")
    store.set("KEY_a", "secret-a")
    account = Account(id=account_id, project_id="deploysafe", name="orphan",
                      platform="tiktok", handle="@orphan", publisher="postpeer")

    with pytest.raises(LookupError, match="no enabled postpeer credential"):
        Registry(conn, store)(account)


def test_an_unknown_balance_is_untried_not_empty(conn, tmp_path):
    """A freshly registered credential has credits_left NULL. Reading that as
    zero would make it unusable until someone set a number by hand."""
    seed_credentials(conn, [("a", None, True)], checked_at=None)
    assert pool(conn, tmp_path).select("deploysafe", "tiktok").label == "a"


def test_a_known_balance_is_preferred_over_an_unknown_one(conn, tmp_path):
    seed_credentials(conn, [("a", None, True)], checked_at=None)
    seed_credentials(conn, [("b", 4, True)])
    assert pool(conn, tmp_path).select("deploysafe", "tiktok").label == "b"


def test_an_exhausted_credential_comes_back_next_month(conn, tmp_path):
    """Credits reset monthly. Without this the rotation permanently loses an
    account the first time it runs dry."""
    seed_credentials(conn, [("a", 0, True)],
                     checked_at=NOW - timedelta(days=40))
    chosen = pool(conn, tmp_path).select("deploysafe", "tiktok")

    assert chosen.label == "a"
    assert chosen.credits_left is None


def test_a_count_from_this_month_is_not_reset(conn, tmp_path):
    seed_credentials(conn, [("a", 0, True)], checked_at=NOW - timedelta(days=1))
    with pytest.raises(OutOfCreditError):
        pool(conn, tmp_path).select("deploysafe", "tiktok")


def test_usage_is_never_polled_unless_explicitly_wired(conn, tmp_path):
    """§8.2.1(1)'s 15-minute refresh costs more credits than it saves: the
    call is 1 credit and a free month is 20."""
    seed_credentials(conn, [("a", None, True)], checked_at=None)
    p = pool(conn, tmp_path)          # no fetch_usage
    assert p.fetch_usage is None
    p.select("deploysafe", "tiktok")  # must not raise, must not call anything
