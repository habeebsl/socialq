"""Caption assembly. §1.

`text_for` is a port of video-maker's `Post.text_for()` and must stay
byte-identical to it. The expected strings below are the original's own output,
so this file is the regression test for the port: if a tidy-up changes one of
these, the two repos have diverged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from socialq.models import Post
from socialq.text import text_for

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def post(**kw):
    defaults = dict(
        id=1, project_id="deploysafe", external_id="x", pipeline="brainrot",
        caption={"default": "the caption"}, scheduled_for=NOW,
    )
    return Post(**{**defaults, **kw})


def test_all_three_blocks():
    p = post(cta={"instagram": "link in bio"}, hashtags={"instagram": ["#a", "#b"]})
    assert text_for(p, "instagram") == "the caption\n\nlink in bio\n\n#a #b"


def test_story_has_no_cta_or_tags_so_only_the_caption_survives():
    # §1: a story target carries empty cta and hashtags by design.
    p = post(cta={}, hashtags={})
    assert text_for(p, "instagram") == "the caption"


def test_per_platform_override():
    p = post(caption={"default": "generic", "instagram": "specific"},
             cta={"instagram": "link in bio"})
    assert text_for(p, "instagram") == "specific\n\nlink in bio"


def test_empty_caption_falls_back():
    # `or`, not `is None`: an empty string falls back to default too.
    p = post(caption={"default": "fallback", "instagram": ""})
    assert text_for(p, "instagram") == "fallback"


def test_cta_but_no_tags():
    p = post(caption={"default": "x"}, cta={"instagram": "scan at deploysafe.io"})
    assert text_for(p, "instagram") == "x\n\nscan at deploysafe.io"
    # A post with no tags does not end in whitespace.
    assert not text_for(p, "instagram").endswith(("\n", " "))


def test_unknown_platform():
    p = post(caption={"default": "x"}, cta={"instagram": "scan at deploysafe.io"},
             hashtags={"instagram": ["#a"]})
    assert text_for(p, "linkedin") == "x"


def test_cta_and_hashtags_have_no_default_fallback():
    # Asymmetric with caption on purpose: a CTA is per-platform or absent.
    p = post(caption={"default": "x"}, cta={"default": "link in bio"},
             hashtags={"default": ["#a"]})
    assert text_for(p, "instagram") == "x"
