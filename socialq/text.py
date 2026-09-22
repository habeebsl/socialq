"""Caption assembly. §1.

    [caption]

    [cta]

    [hashtags]

Three blocks, blank line between each, empty blocks dropped.

`text_for` is a port of `video-maker`'s `Post.text_for()`, kept deliberately
line-for-line with the original so the two stay byte-identical (§1). Resist
tidying it: the `or` fallbacks and the asymmetry between caption and cta are
the behaviour, not an accident.

    caption  platform key, falling back to "default" when absent OR empty
    cta      platform key only -- no "default" fallback
    hashtags platform key only, joined on a single space
"""

from __future__ import annotations

from .models import Post

SEPARATOR = "\n\n"


def text_for(post: Post, platform: str) -> str:
    """The finished post text: caption, then CTA, then hashtags.

    Ported verbatim from video-maker.
    """
    blocks = [
        post.caption.get(platform) or post.caption.get("default", ""),
        post.cta.get(platform, ""),
        " ".join(post.hashtags.get(platform) or []),
    ]
    return SEPARATOR.join(b.strip() for b in blocks if b and b.strip())


# §1: a story target has empty cta and hashtags by design, so text_for already
# yields just the caption for one. Whether that text reaches the platform is the
# publisher's business -- Instagram accepts none for a story -- not this
# function's, which stays a faithful copy of the original.
