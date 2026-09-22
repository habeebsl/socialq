"""Row types.

Plain dataclasses, constructed from dict rows. No ORM: the queries that matter
(§5's claim, §7's reconciliation sweep) are hand-written SQL, and an ORM would
only obscure them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TargetState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    IN_FLIGHT = "in_flight"
    PUBLISHED = "published"
    FAILED = "failed"
    DEAD = "dead"


class Surface(StrEnum):
    POST = "post"
    STORY = "story"


@dataclass(frozen=True)
class Account:
    id: int
    project_id: str
    name: str
    platform: str
    handle: str
    publisher: str
    external_id: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class Post:
    id: int
    project_id: str
    external_id: str
    pipeline: str
    caption: dict[str, str]
    scheduled_for: datetime
    cta: dict[str, str] = field(default_factory=dict)
    hashtags: dict[str, list[str]] = field(default_factory=dict)
    media_ids: list[int] = field(default_factory=list)
    aigc: bool = False


@dataclass(frozen=True)
class Target:
    id: int
    post_id: int
    account_id: int
    surface: str = Surface.POST
    state: str = TargetState.PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    claimed_at: datetime | None = None
    claimed_by: str | None = None
    provider_post_id: str | None = None
    permalink: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class Result:
    """What a successful publish yields."""

    provider_post_id: str
    permalink: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemotePost:
    """A post as the platform reports it. Used by reconciliation (§7).

    `media_url` is whatever the platform reports, which is not necessarily the
    URL we submitted: Instagram re-hosts media on its own CDN, so only PostPeer
    echoes ours back. `caption` is therefore the identifying field on
    Instagram -- see socialq/reconcile.py.
    """

    provider_post_id: str
    created_at: datetime | None = None
    permalink: str | None = None
    media_url: str | None = None
    caption: str | None = None
