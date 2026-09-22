"""Publishers — one class per platform, swappable. §8.

The boundary is the point of the design (§3): PostPeer must stay a detail of
one file. The day a native TikTok client gets audited, that is a new Publisher
subclass and a routing change; the queue, worker and SDK never learn about it.
"""

from .base import (
    AuthError,
    OutOfCreditError,
    Publisher,
    PublishError,
    RateLimitedError,
    Retryable,
)

__all__ = [
    "AuthError",
    "OutOfCreditError",
    "Publisher",
    "PublishError",
    "RateLimitedError",
    "Retryable",
]
