"""PostPeer — TikTok. §8.2.

TikTok's own API is effectively closed to a script: Direct Post requires an
audit whose demo video must show a UI that a headless job does not have, and an
unaudited client is refused outright with
`unaudited_client_can_only_post_to_private_accounts` (§2). PostPeer's client is
pre-approved, so TikTok goes through them.

This file is the only place that knows that. §3: the day a native TikTok client
gets audited, that is a new Publisher subclass and a routing change.

Verified against https://www.postpeer.dev/docs/platforms/tiktok
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from ..media import Media
from ..models import Account, Post, RemotePost, Result, Target
from ..text import text_for
from .base import (
    AuthError,
    OutOfCreditError,
    PublishError,
    RateLimitedError,
    Retryable,
)

log = logging.getLogger("socialq.postpeer")

BASE_URL = "https://api.postpeer.dev/v1"

# §8.2: 1-32 images, two or more make a carousel; aspect ratio between 1:2.13
# and 2.13:1 and all images must match (checked by the producer, not here --
# socialq does not open media files).
MAX_CAROUSEL_IMAGES = 32

# A photo post's `content` is the title, not the body.
MAX_PHOTO_TITLE = 90
MAX_PHOTO_DESCRIPTION = 4000
MAX_VIDEO_CONTENT = 2200

PRIVACY_PUBLIC = "PUBLIC_TO_EVERYONE"

_AUTH_MARKERS = ("unauthorized", "invalid api key", "invalid access key",
                 "forbidden", "access key")
_CREDIT_MARKERS = ("credit", "quota exceeded", "insufficient", "upgrade your plan")


class PostPeerTikTok:
    """Publishes one target through one PostPeer credential.

    Constructed per target so the credential is fixed before the attempt row is
    written -- §8.2.1(4) requires knowing which account posted, and a crash
    must not leave that unanswered.
    """

    name = "postpeer"
    platform = "tiktok"

    def __init__(
        self,
        api_key: str,
        account_id: str,
        *,
        credential_id: int | None = None,
        client: httpx.Client | None = None,
        draft: bool = False,
        pool=None,
        credential=None,
    ):
        self.api_key = api_key
        self.account_id = account_id
        # Read by the worker before it commits the attempt row.
        self.credential_id = credential_id
        self._client = client or httpx.Client(timeout=120.0)
        self.draft = draft
        self._pool = pool
        self._credential = credential

    # -- transport ---------------------------------------------------------

    def _call(self, method: str, path: str, json=None, params=None) -> dict:
        """One call, with failures in a 200 body raised as exceptions (§13)."""
        try:
            response = self._client.request(
                method,
                f"{BASE_URL}/{path.lstrip('/')}",
                json=json,
                params=params,
                headers={"x-access-key": self.api_key},
            )
        except httpx.RequestError as exc:
            raise Retryable(f"postpeer: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.status_code in (401, 403):
            raise AuthError(f"postpeer: HTTP {response.status_code}", raw=body)
        if response.status_code == 402:
            raise OutOfCreditError("postpeer: payment required", raw=body)
        if response.status_code == 429:
            raise RateLimitedError("postpeer: rate limited", raw=body)
        if response.status_code >= 500:
            raise Retryable(f"postpeer: HTTP {response.status_code}", raw=body)
        if response.status_code >= 400:
            raise self._error_for(_message(body), body)
        if not isinstance(body, dict):
            raise Retryable("postpeer: unexpected response shape", raw={"body": body})
        return body

    @staticmethod
    def _error_for(message: str, body: dict) -> PublishError:
        """Classify by message: the API reports failures in prose, not codes."""
        lowered = (message or "").lower()
        if any(marker in lowered for marker in _CREDIT_MARKERS):
            # §8.2.1: "try the next credential", not a retry of this one.
            return OutOfCreditError(f"postpeer: {message}", raw=body)
        if any(marker in lowered for marker in _AUTH_MARKERS):
            return AuthError(f"postpeer: {message}", raw=body)
        return PublishError(f"postpeer: {message or 'unknown error'}", raw=body)

    # -- validation --------------------------------------------------------

    @classmethod
    def check(cls, media: list[Media], text: str, title: str) -> None:
        if not media:
            raise PublishError("postpeer: nothing to post")

        videos = [m for m in media if m.mime.startswith("video/")]
        images = [m for m in media if m.mime.startswith("image/")]
        if videos and images:
            raise PublishError("postpeer: a TikTok post is video or photos, not both")
        if len(videos) > 1:
            raise PublishError("postpeer: one video per TikTok post")
        if len(images) > MAX_CAROUSEL_IMAGES:
            raise PublishError(
                f"postpeer: {len(images)} images exceeds {MAX_CAROUSEL_IMAGES}"
            )

        if images:
            if len(title) > MAX_PHOTO_TITLE:
                raise PublishError(
                    f"postpeer: photo title is {len(title)} chars, "
                    f"limit is {MAX_PHOTO_TITLE}"
                )
            if len(text) > MAX_PHOTO_DESCRIPTION:
                raise PublishError(
                    f"postpeer: description is {len(text)} chars, "
                    f"limit is {MAX_PHOTO_DESCRIPTION}"
                )
        elif len(text) > MAX_VIDEO_CONTENT:
            raise PublishError(
                f"postpeer: content is {len(text)} chars, limit is {MAX_VIDEO_CONTENT}"
            )

    # -- publishing --------------------------------------------------------

    def build_request(self, post: Post, media: list[Media]) -> dict:
        text = text_for(post, self.platform)
        is_photo = bool(media) and not media[0].mime.startswith("video/")
        # §8.2: for a carousel, `content` is the 90-char title and the body goes
        # in `description`. The title is the caption alone -- appending a CTA
        # and hashtags to a 90-char field would truncate the words that matter.
        title = (post.caption.get(self.platform)
                 or post.caption.get("default", "")).strip()
        self.check(media, text, title if is_photo else "")

        platform_data: dict[str, object] = {
            "privacyLevel": PRIVACY_PUBLIC,
            "disableComment": False,
            "disableDuet": False,
            "disableStitch": False,
            # §8.2: synthetic voices require disclosure, and undisclosed AI
            # content is a takedown risk.
            "isAigc": bool(post.aigc),
            "draft": self.draft,
        }
        if is_photo:
            platform_data["description"] = text
            platform_data["photoCoverIndex"] = 0

        return {
            "content": title if is_photo else text,
            "mediaItems": [
                {
                    "type": "video" if item.mime.startswith("video/") else "image",
                    "url": item.url,
                }
                for item in media
            ],
            "platforms": [
                {
                    "platform": self.platform,
                    "accountId": self.account_id,
                    "platformSpecificData": platform_data,
                }
            ],
            "publishNow": True,
        }

    def publish(self, target: Target, post: Post, media: list[Media]) -> Result:
        body = self._call("POST", "posts", json=self.build_request(post, media))

        # §13: a 200 can still be a failure. Check the envelope and the
        # per-platform result, because either can say no on its own.
        entry = _platform_entry(body, self.platform)
        if body.get("success") is False or (entry and entry.get("success") is False):
            raise self._error_for(_message(entry or body), body)

        post_id = body.get("postId") or body.get("id")
        if not post_id:
            raise PublishError("postpeer: no postId in response", raw=body)

        if self._pool is not None and self._credential is not None:
            self._pool.spend(self._credential, 1)

        return Result(
            provider_post_id=str(post_id),
            permalink=(entry or {}).get("platformPostUrl"),
            raw=body,
        )

    # -- reconciliation ----------------------------------------------------

    def find_recent(self, account: Account, since: datetime) -> list[RemotePost]:
        """§7: match on the media URL, which PostPeer echoes back unchanged.

        §8.2.1(4): this must run against the same credential that published, so
        the publisher is rebuilt from the attempt's credential_id, not from
        whichever account happens to have the most credits now.
        """
        body = self._call("GET", "posts/", params={"limit": 50})
        found = []
        for item in _listing(body):
            created = _parse_timestamp(
                item.get("createdAt") or item.get("created_at")
                or item.get("publishedAt")
            )
            if created is not None and created < since:
                continue
            entry = _platform_entry(item, self.platform) or {}
            for media_item in item.get("mediaItems") or []:
                found.append(
                    RemotePost(
                        provider_post_id=str(
                            item.get("postId") or item.get("id") or ""
                        ),
                        created_at=created,
                        permalink=entry.get("platformPostUrl"),
                        media_url=media_item.get("url"),
                        caption=item.get("content"),
                    )
                )
        return found

    def credits_remaining(self) -> int:
        """§8.2.1(1). Costs a credit itself, so call it on a schedule."""
        body = self._call("GET", "usage/")
        for key in ("creditsRemaining", "credits_remaining", "remaining",
                    "creditsLeft", "credits_left"):
            if key in body:
                return int(body[key])
        usage = body.get("usage") or body.get("data") or {}
        for key in ("creditsRemaining", "remaining", "creditsLeft"):
            if key in usage:
                return int(usage[key])
        total, used = usage.get("quota") or body.get("quota"), usage.get("used")
        if total is not None and used is not None:
            return int(total) - int(used)
        raise Retryable("postpeer: could not read credits from usage", raw=body)

    def integrations(self) -> list[dict]:
        """GET /v1/connect/integrations -- where accountId comes from."""
        body = self._call("GET", "connect/integrations")
        return _listing(body)


# -- response helpers ----------------------------------------------------
#
# Shapes are read defensively: the published docs show the success case only,
# and a KeyError in the worker would be a far worse failure than a miss here.


def _listing(body) -> list[dict]:
    if isinstance(body, list):
        return body
    for key in ("posts", "data", "results", "integrations", "items"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    return []


def _platform_entry(body, platform: str) -> dict | None:
    if not isinstance(body, dict):
        return None
    for entry in body.get("platforms") or []:
        if isinstance(entry, dict) and entry.get("platform") == platform:
            return entry
    return None


def _message(body) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("error", "message", "detail", "errorMessage"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("message") or value.get("detail")
            if isinstance(nested, str) and nested:
                return nested
    return ""


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
