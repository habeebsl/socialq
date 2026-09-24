"""The SDK. §10 — what video-maker imports.

It writes to Postgres directly. There is no HTTP API in v1: the only producer
is Python on the same machine, and an API would add a deploy, an auth surface
and a second thing that can be down, to serve a caller that does not need it.
The signature is what matters -- it stays the same if a transport is ever added
underneath it (§12).

    sq = Client()
    sq.enqueue(
        project="deploysafe",
        pipeline="brainrot",
        external_id="api-no-auth",
        media=["output/brainrot/api-no-auth.mp4"],
        caption={"default": "..."},
        cta={"instagram": "link in bio"},
        hashtags={"instagram": ["#vibecoding"]},
        surface={"instagram": "post"},
        accounts={"instagram": ["deployedunsafe"]},
        aigc=True,
        at="2026-09-22T14:00:00Z",
    )
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import R2Config, database_url
from .media import Media, ensure_media, is_url, media_for_url
from .models import Post, Surface
from .publishers.instagram import InstagramGraph
from .publishers.postpeer import PostPeerTikTok
from .text import text_for

log = logging.getLogger("socialq.sdk")

KNOWN_PLATFORMS = {"instagram", "tiktok", "x", "linkedin"}


class ValidationError(ValueError):
    """Rejected in front of the caller.

    §10: failing here beats failing in the worker six hours later with nobody
    watching.
    """


@dataclass
class EnqueueResult:
    post_id: int
    targets: list[int] = field(default_factory=list)
    media_ids: list[int] = field(default_factory=list)
    created: bool = True

    def __bool__(self) -> bool:
        return True


class Client:
    """Writes finished content into the queue."""

    def __init__(
        self,
        url: str | None = None,
        *,
        store=None,
        base_dir: Path | str | None = None,
    ):
        self._url = url
        self._store = store
        # §1: media paths are repo-relative. Resolve against the producer's
        # checkout, which is the working directory unless told otherwise.
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()

    @property
    def store(self):
        if self._store is None:
            from .media import MediaStore

            self._store = MediaStore(R2Config.from_env())
        return self._store

    def upload(self, path: str | Path, *, project: str) -> str:
        """Store a file now and register it, returning its public URL.

        For producers that render long before they publish: the URL exists
        immediately, so a rendered-but-unpublished file can be previewed and is
        recorded as still existing. Pass that URL to `enqueue` later instead of
        the path, and the bytes are never touched again -- which is what allows
        publishing from a different machine than the one that rendered it.

        Idempotent on content: the same bytes upload once, however often this
        is called.
        """
        from .media import upload as upload_media

        with psycopg.connect(self._url or database_url(), row_factory=dict_row,
                             prepare_threshold=None) as conn:
            return upload_media(
                self._resolve(path), project=project, conn=conn, store=self.store
            )

    def enqueue(
        self,
        *,
        project: str,
        pipeline: str,
        external_id: str,
        media: list[str | Path],
        caption: dict[str, str],
        surface: dict[str, str],
        accounts: dict[str, list[str]],
        cta: dict[str, str] | None = None,
        hashtags: dict[str, list[str]] | None = None,
        aigc: bool = False,
        at: str | datetime | None = None,
    ) -> EnqueueResult:
        """Insert one post and its targets.

        `accounts` is required and names every destination explicitly:

            surface  = {"instagram": "post", "tiktok": "post"}
            accounts = {"instagram": ["deployedunsafe"], "tiktok": ["deployedunsafe"]}

        It is not optional and has no "all accounts on this platform" default
        on purpose. Defaulting would mean registering a second account
        silently changes where every existing caller posts -- a surprise that
        surfaces as an unexpected post on a real account, which is not a thing
        to discover after the fact.

        Idempotent on (project, pipeline, external_id) -- re-running a batch
        must not double-post. A partial enqueue must be impossible, so all of
        it happens in one transaction.
        """
        cta = cta or {}
        hashtags = hashtags or {}
        scheduled_for = _parse_when(at)

        validate(
            caption=caption, surface=surface, cta=cta, hashtags=hashtags,
            media=media, aigc=aigc, accounts=accounts,
        )

        with psycopg.connect(self._url or database_url(), row_factory=dict_row,
                             prepare_threshold=None) as conn:
            with conn.transaction():
                existing = conn.execute(
                    "SELECT id FROM posts WHERE project_id = %s AND pipeline = %s"
                    " AND external_id = %s",
                    (project, pipeline, external_id),
                ).fetchone()
                if existing:
                    # Already enqueued. Return what is there rather than
                    # inserting a second post or a second set of targets.
                    log.info(
                        "post %s/%s/%s already enqueued as %s",
                        project, pipeline, external_id, existing["id"],
                    )
                    return self._existing(conn, existing["id"])

                conn.execute(
                    "INSERT INTO projects (id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (project,),
                )
                items = [self._media(conn, project, entry) for entry in media]

                post_row = conn.execute(
                    "INSERT INTO posts (project_id, external_id, pipeline, caption,"
                    " cta, hashtags, media_ids, aigc, scheduled_for)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (project, external_id, pipeline, json.dumps(caption),
                     json.dumps(cta), json.dumps(hashtags),
                     [item.id for item in items], aigc, scheduled_for),
                ).fetchone()
                post_id = post_row["id"]

                post = Post(
                    id=post_id, project_id=project, external_id=external_id,
                    pipeline=pipeline, caption=caption, cta=cta,
                    hashtags=hashtags, media_ids=[i.id for i in items],
                    aigc=aigc, scheduled_for=scheduled_for,
                )
                target_ids = self._targets(
                    conn, project, post, items, surface, accounts
                )

        return EnqueueResult(
            post_id=post_id,
            targets=target_ids,
            media_ids=[item.id for item in items],
            created=True,
        )

    # -- internals ---------------------------------------------------------

    def _media(self, conn, project: str, entry) -> Media:
        """One `media` entry: an already-registered URL, or a local path.

        A URL is looked up, never fetched. The producer passing it is asserting
        it already registered the object, and downloading the bytes to verify
        would undo the reason for passing a URL.
        """
        if not is_url(entry):
            return ensure_media(conn, project, self._resolve(entry), self.store)

        found = media_for_url(conn, project, entry)
        if found is None:
            raise ValidationError(
                f"{entry} is not registered for project {project}; upload it "
                "first with Client.upload(path, project=...), or pass the file "
                "path instead"
            )
        return found

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.base_dir / candidate

    def _targets(
        self,
        conn: psycopg.Connection,
        project: str,
        post: Post,
        media: list[Media],
        surface: dict[str, str],
        accounts: dict[str, list[str]],
    ) -> list[int]:
        """One target per (named account, surface).

        Disabled accounts still get a row: §14's warming means an account can
        be registered and held back, and the claim query is what skips it.
        """
        target_ids = []
        for platform, kind in surface.items():
            wanted = accounts[platform]
            rows = conn.execute(
                "SELECT id, name, publisher FROM accounts WHERE project_id = %s"
                " AND platform = %s AND name = ANY(%s) ORDER BY id",
                (project, platform, wanted),
            ).fetchall()

            found = {row["name"] for row in rows}
            missing = [name for name in wanted if name not in found]
            if missing:
                available = [r["name"] for r in conn.execute(
                    "SELECT name FROM accounts WHERE project_id = %s"
                    " AND platform = %s ORDER BY name", (project, platform),
                ).fetchall()]
                raise ValidationError(
                    f"no {platform} account named {missing} in project "
                    f"{project}; registered: {available or 'none'}"
                )

            check_platform(post, media, platform, kind)

            for account in rows:
                row = conn.execute(
                    "INSERT INTO targets (post_id, account_id, surface)"
                    " VALUES (%s,%s,%s)"
                    " ON CONFLICT (post_id, account_id, surface) DO NOTHING"
                    " RETURNING id",
                    (post.id, account["id"], kind),
                ).fetchone()
                if row:
                    target_ids.append(row["id"])
        return target_ids

    def _existing(self, conn: psycopg.Connection, post_id: int) -> EnqueueResult:
        row = conn.execute(
            "SELECT media_ids FROM posts WHERE id = %s", (post_id,)
        ).fetchone()
        targets = conn.execute(
            "SELECT id FROM targets WHERE post_id = %s ORDER BY id", (post_id,)
        ).fetchall()
        return EnqueueResult(
            post_id=post_id,
            targets=[t["id"] for t in targets],
            media_ids=list(row["media_ids"]),
            created=False,
        )


# -- validation ----------------------------------------------------------


def validate(*, caption, surface, cta, hashtags, media, aigc, accounts) -> None:
    """Shape checks that do not need the database or the media files."""
    if not media:
        raise ValidationError("no media")
    if not caption:
        raise ValidationError("no caption")
    if not surface:
        raise ValidationError("no surface: nothing would be posted anywhere")
    if not accounts:
        raise ValidationError(
            "no accounts: name every destination explicitly, e.g. "
            '{"instagram": ["deployedunsafe"]}'
        )

    # surface and accounts must describe the same set of platforms. A platform
    # in one but not the other is a typo that would either post nowhere or
    # post without a surface -- both silent.
    only_surface = set(surface) - set(accounts)
    only_accounts = set(accounts) - set(surface)
    if only_surface:
        raise ValidationError(
            f"surface names {sorted(only_surface)} but accounts does not; "
            "every platform needs its destinations listed"
        )
    if only_accounts:
        raise ValidationError(
            f"accounts names {sorted(only_accounts)} but surface does not; "
            "every platform needs a surface"
        )
    for platform, names in accounts.items():
        if not names:
            raise ValidationError(f"accounts[{platform!r}] is empty")
        if isinstance(names, str):
            raise ValidationError(
                f"accounts[{platform!r}] must be a list, not a string"
            )
    if not isinstance(aigc, bool):
        raise ValidationError("aigc must be a bool")

    for platform in surface:
        if platform not in KNOWN_PLATFORMS:
            raise ValidationError(
                f"unknown platform {platform!r}; known: {sorted(KNOWN_PLATFORMS)}"
            )
    for platform, kind in surface.items():
        if kind not in (Surface.POST, Surface.STORY):
            raise ValidationError(
                f"{platform}: surface must be 'post' or 'story', not {kind!r}"
            )

    # A caption or CTA keyed to a platform nothing posts to is a typo, and a
    # silent one: the text simply never appears.
    for name, mapping in (("caption", caption), ("cta", cta),
                          ("hashtags", hashtags)):
        for key in mapping:
            if key != "default" and key not in surface:
                raise ValidationError(
                    f"{name} has a key for {key!r}, which is not in surface"
                )

    for entry in media:
        if not str(entry).strip():
            raise ValidationError("empty media path")


def check_platform(post: Post, media: list[Media], platform: str, kind: str) -> None:
    """Ask the publisher itself whether it would accept this.

    The limits live with the publisher that enforces them (§8), so the SDK does
    not carry a second, drifting copy of Instagram's caption cap.
    """
    text = "" if kind == Surface.STORY else text_for(post, platform)
    if platform == "instagram":
        InstagramGraph.check(media, text, kind)
    elif platform == "tiktok":
        if kind == Surface.STORY:
            raise ValidationError("tiktok has no story surface")
        title = (post.caption.get(platform)
                 or post.caption.get("default", "")).strip()
        is_photo = bool(media) and not media[0].mime.startswith("video/")
        PostPeerTikTok.check(media, text, title if is_photo else "")


def _parse_when(at: str | datetime | None) -> datetime:
    if at is None:
        return datetime.now(timezone.utc)
    if isinstance(at, datetime):
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"cannot parse `at`: {at!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
