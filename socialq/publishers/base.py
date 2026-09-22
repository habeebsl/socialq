"""The publisher interface. §8.1."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..media import Media
from ..models import Account, Post, RemotePost, Result, Target


class PublishError(Exception):
    """A publish failed. Retryable unless a subclass says otherwise."""

    retryable = True

    def __init__(self, message: str, *, raw: dict | None = None):
        super().__init__(message)
        self.raw = raw or {}


class Retryable(PublishError):
    """Transient: a timeout, a 5xx, a container that never finished."""


class AuthError(PublishError):
    """The credential is revoked, expired or wrong.

    Not retryable with the same credential. §8.2.1: mark it disabled and carry
    on with the others -- one dead account must never stall the queue.
    """

    retryable = False


class RateLimitedError(PublishError):
    """The platform's own ceiling, e.g. Instagram's 50 posts/24h (§8.3)."""

    def __init__(self, message: str, *, retry_after: datetime | None = None, raw=None):
        super().__init__(message, raw=raw)
        self.retry_after = retry_after


class OutOfCreditError(PublishError):
    """No credit left on this credential. §8.2.1.

    Running out is a wait, not an error: the worker tries the next credential,
    and if none has credit the target stays pending until next month.
    """

    retryable = False


@runtime_checkable
class Publisher(Protocol):
    """§8.1. `find_recent` exists for §7 -- a publisher without it cannot be
    reconciled, so it is part of the interface rather than an optional extra."""

    name: str

    def publish(
        self, target: Target, post: Post, media: list[Media]
    ) -> Result: ...

    def find_recent(
        self, account: Account, since: datetime
    ) -> list[RemotePost]: ...
