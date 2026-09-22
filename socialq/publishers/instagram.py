"""Instagram Graph — direct, no vendor. §8.3.

Instagram API with Instagram Login (not the Facebook-Login route). Posting to
your own account keeps the Meta app in development mode: no app review, no demo
video, no business verification (§2).

    POST /{ig-user-id}/media           -> creation_id     (container)
    GET  /{creation_id}?fields=status_code   until FINISHED
    POST /{ig-user-id}/media_publish   -> media id

The polling is not optional: publishing a container that is still IN_PROGRESS
fails, and video containers are never ready immediately.

Verified against
https://developers.facebook.com/docs/instagram-platform/content-publishing
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx

from ..media import Media
from ..models import Account, Post, RemotePost, Result, Surface, Target
from ..text import text_for
from .base import AuthError, PublishError, RateLimitedError, Retryable

API_HOST = "https://graph.instagram.com"
API_VERSION = "v25.0"

# "JPEG is the only image format supported. Extended JPEG formats such as MPO
# and JPS are not supported." PNG and WebP are rejected by Instagram, so catch
# them here rather than as a container ERROR minutes later.
ALLOWED_IMAGE_MIMES = {"image/jpeg"}

# "A comma separated list of up to 10 container IDs."
MAX_CAROUSEL_ITEMS = 10

# 2,200 characters, no more than 30 hashtags.
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30

# The published 24h ceiling has been documented as both 50 and 100 depending on
# the page, so nothing here hardcodes it -- publishing_limit() reads the real
# quota_total off the account (§8.3).
_SUCCESS_STATUSES = {"FINISHED", "PUBLISHED"}
_TERMINAL_STATUSES = _SUCCESS_STATUSES | {"ERROR", "EXPIRED"}

# Meta error codes meaning "this token will never work again" rather than
# "try later". §8.2.1's rule applies here too: disable, don't spin.
_AUTH_ERROR_CODES = {190, 102, 10, 200, 2500}
_RATE_LIMIT_CODES = {4, 17, 32, 613}
_TRANSIENT_CODES = {1, 2}


class InstagramGraph:
    """Publishes to one Instagram account.

    The token is per-account, so one instance serves one account. §8.3's 60-day
    token clock is handled by the reconcile job, not here.
    """

    name = "instagram_graph"

    def __init__(
        self,
        access_token: str,
        ig_user_id: str,
        *,
        client: httpx.Client | None = None,
        api_version: str = API_VERSION,
        poll_interval: float = 3.0,
        poll_timeout: float = 300.0,
    ):
        self.access_token = access_token
        self.ig_user_id = ig_user_id
        self.base_url = f"{API_HOST}/{api_version}"
        self._client = client or httpx.Client(timeout=60.0)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    # -- transport ---------------------------------------------------------

    def _call(self, method: str, path: str, **params) -> dict:
        """One Graph call, with Meta's in-body errors raised as exceptions.

        §13: both APIs return errors in a 200 body, so a check on the status
        code alone proves nothing.
        """
        params["access_token"] = self.access_token
        try:
            response = self._client.request(
                method, f"{self.base_url}/{path.lstrip('/')}", params=params
            )
        except httpx.RequestError as exc:
            raise Retryable(f"instagram: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            raise Retryable(
                f"instagram: non-JSON response ({response.status_code})"
            ) from None

        if isinstance(body, dict) and "error" in body:
            raise self._error_for(body["error"], body)
        if response.status_code >= 500:
            raise Retryable(f"instagram: HTTP {response.status_code}", raw=body)
        if response.status_code >= 400:
            raise PublishError(f"instagram: HTTP {response.status_code}", raw=body)
        return body

    @staticmethod
    def _error_for(error: dict, body: dict) -> PublishError:
        code = error.get("code")
        message = error.get("message", "unknown error")
        if code in _AUTH_ERROR_CODES:
            return AuthError(f"instagram: {message}", raw=body)
        if code in _RATE_LIMIT_CODES:
            return RateLimitedError(f"instagram: {message}", raw=body)
        if code in _TRANSIENT_CODES:
            return Retryable(f"instagram: {message}", raw=body)
        return PublishError(f"instagram: {message}", raw=body)

    # -- validation --------------------------------------------------------

    @staticmethod
    def check(media: list[Media], caption: str, surface: str) -> None:
        """Reject what Instagram will reject, before anything is uploaded.

        §10: failing in front of the caller beats failing in the worker six
        hours later with nobody watching.
        """
        if not media:
            raise PublishError("instagram: nothing to post")

        for item in media:
            if item.mime.startswith("image/") and item.mime not in ALLOWED_IMAGE_MIMES:
                raise PublishError(
                    f"instagram: {item.mime} images are not supported; "
                    "JPEG is the only accepted image format"
                )

        if surface == Surface.STORY:
            if len(media) != 1:
                raise PublishError("instagram: a story takes exactly one item")
            return

        if len(media) > MAX_CAROUSEL_ITEMS:
            raise PublishError(
                f"instagram: {len(media)} items exceeds the "
                f"{MAX_CAROUSEL_ITEMS}-item carousel limit"
            )
        if len(caption) > MAX_CAPTION_CHARS:
            raise PublishError(
                f"instagram: caption is {len(caption)} chars, "
                f"limit is {MAX_CAPTION_CHARS}"
            )
        if caption.count("#") > MAX_HASHTAGS:
            raise PublishError(
                f"instagram: {caption.count('#')} hashtags exceeds {MAX_HASHTAGS}"
            )

    # -- publishing --------------------------------------------------------

    def publish(self, target: Target, post: Post, media: list[Media]) -> Result:
        story = target.surface == Surface.STORY
        # §1/§8.3: Instagram accepts no text for a story through the API, so
        # the caption is built but deliberately not sent.
        caption = "" if story else text_for(post, "instagram")
        self.check(media, caption, target.surface)

        if story:
            container = self._create_container(
                media[0], media_type="STORIES", aigc=post.aigc
            )
        elif len(media) == 1:
            container = self._create_container(
                media[0], caption=caption, aigc=post.aigc
            )
        else:
            container = self._create_carousel(media, caption, aigc=post.aigc)

        self._await_container(container)
        published = self._call(
            "POST", f"{self.ig_user_id}/media_publish", creation_id=container
        )
        media_id = published.get("id")
        if not media_id:
            raise PublishError("instagram: media_publish returned no id", raw=published)

        return Result(
            provider_post_id=str(media_id),
            permalink=self._permalink(str(media_id)),
            raw=published,
        )

    def _create_container(
        self,
        item: Media,
        *,
        caption: str | None = None,
        media_type: str | None = None,
        carousel_item: bool = False,
        aigc: bool = False,
    ) -> str:
        params: dict[str, object] = {}
        is_video = item.mime.startswith("video/")

        if is_video:
            params["video_url"] = item.url
            if media_type:
                params["media_type"] = media_type
            elif carousel_item:
                # REELS is invalid for a carousel child; VIDEO is the documented
                # value for one.
                params["media_type"] = "VIDEO"
            else:
                # A standalone video post is published as a Reel.
                params["media_type"] = "REELS"
        else:
            params["image_url"] = item.url
            if media_type:
                params["media_type"] = media_type

        if carousel_item:
            params["is_carousel_item"] = "true"
        else:
            if caption:
                params["caption"] = caption
            # Self-disclosure of AI usage. §2 treats undisclosed synthetic media
            # as a takedown risk; the docs put this on the parent only.
            if aigc:
                params["is_ai_generated"] = "true"

        body = self._call("POST", f"{self.ig_user_id}/media", **params)
        container = body.get("id")
        if not container:
            raise PublishError("instagram: no creation_id returned", raw=body)
        return str(container)

    def _create_carousel(
        self, media: list[Media], caption: str, *, aigc: bool = False
    ) -> str:
        children = [self._create_container(item, carousel_item=True) for item in media]
        for child in children:
            self._await_container(child)

        params: dict[str, object] = {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
        }
        if caption:
            params["caption"] = caption
        if aigc:
            # "For carousel posts, only the parent container should have this
            # parameter set."
            params["is_ai_generated"] = "true"

        body = self._call("POST", f"{self.ig_user_id}/media", **params)
        container = body.get("id")
        if not container:
            raise PublishError("instagram: no carousel creation_id", raw=body)
        return str(container)

    def _await_container(self, container_id: str) -> None:
        """Poll until FINISHED. Anything else is an error worth the detail."""
        deadline = time.monotonic() + self.poll_timeout
        while True:
            body = self._call("GET", container_id, fields="status_code,status")
            status = body.get("status_code")
            if status in _SUCCESS_STATUSES:
                return
            if status in _TERMINAL_STATUSES:
                # EXPIRED means the container aged out (unpublished for 24h);
                # ERROR is usually media the CDN could not fetch or transcode.
                detail = body.get("status") or status
                raise PublishError(f"instagram: container {status}: {detail}", raw=body)
            if time.monotonic() >= deadline:
                raise Retryable(
                    f"instagram: container still {status} after "
                    f"{self.poll_timeout:.0f}s",
                    raw=body,
                )
            time.sleep(self.poll_interval)

    # -- reconciliation ----------------------------------------------------

    def find_recent(self, account: Account, since: datetime) -> list[RemotePost]:
        """Recent media on the account. §7 matches on media URL and window.

        The edge supports time pagination, so `since` is pushed to the server
        and re-checked here rather than trusted blindly.
        """
        body = self._call(
            "GET",
            f"{self.ig_user_id}/media",
            fields="id,permalink,timestamp,media_url,media_product_type,caption",
            since=int(since.timestamp()),
        )
        found = []
        for item in body.get("data", []):
            created = _parse_timestamp(item.get("timestamp"))
            if created is not None and created < since:
                continue
            found.append(
                RemotePost(
                    provider_post_id=str(item["id"]),
                    created_at=created,
                    permalink=item.get("permalink"),
                    # Instagram's own CDN URL, not the R2 URL we submitted.
                    media_url=item.get("media_url"),
                    caption=item.get("caption"),
                )
            )
        return found

    def refresh_token(self) -> tuple[str, datetime]:
        """Exchange the current long-lived token for a fresh 60 days.

        §8.3/§14. The token must be at least 24h old and still valid; one left
        to lapse past 60 days cannot be refreshed at all and needs a manual
        browser round trip. The endpoint is unversioned.
        """
        try:
            response = self._client.get(
                f"{API_HOST}/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": self.access_token,
                },
            )
            body = response.json()
        except httpx.RequestError as exc:
            raise Retryable(f"instagram: token refresh: {exc}") from exc
        except ValueError:
            raise Retryable("instagram: token refresh returned non-JSON") from None

        if "error" in body:
            raise self._error_for(body["error"], body)
        token = body.get("access_token")
        if not token:
            raise AuthError("instagram: refresh returned no access_token", raw=body)

        expires_in = int(body.get("expires_in", 0))
        return token, utcnow() + timedelta(seconds=expires_in)

    def publishing_limit(self) -> tuple[int, int]:
        """(used, total) posts in the trailing 24h window.

        The total is read from the account rather than hardcoded: Meta's own
        pages disagree on whether it is 50 or 100.
        """
        body = self._call(
            "GET",
            f"{self.ig_user_id}/content_publishing_limit",
            fields="config,quota_usage",
        )
        data = (body.get("data") or [{}])[0]
        config = data.get("config") or {}
        return int(data.get("quota_usage", 0)), int(config.get("quota_total", 0))

    def _permalink(self, media_id: str) -> str | None:
        try:
            body = self._call("GET", media_id, fields="permalink")
        except PublishError:
            # The post exists; we simply could not read its URL back. Losing the
            # permalink must not turn a successful publish into a failure.
            return None
        return body.get("permalink")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("+0000", "+00:00"))
    except ValueError:
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
