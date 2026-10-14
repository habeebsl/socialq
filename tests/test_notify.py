"""Caption emails for drafts.

TikTok's video draft endpoint takes only the file, so the caption is emailed
for pasting. What matters: it is sent once, retried until it is sent, and can
never disturb the post it describes.
"""

from __future__ import annotations

import httpx
import pytest

from socialq.notify import Notifier, NotifyError, notify_drafts, subject_for
from socialq.worker import Worker


class Recorder:
    """A Notifier that remembers, and can be told to fail."""

    recipient = "me@example.com"

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, subject, text, *, idempotency_key):
        if self.fail:
            raise NotifyError("resend: down")
        self.sent.append((subject, text, idempotency_key))


def delivered(conn, *, draft=True, name="johnsecurity", state="published",
              finished="now()"):
    """A target in `state` on an account, with one finished attempt."""
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    account = conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher,"
        " draft) VALUES ('deploysafe', %s, 'tiktok', %s, 'postpeer', %s)"
        " RETURNING id", (name, f"@{name}", draft),
    ).fetchone()["id"]
    post = conn.execute(
        "INSERT INTO posts (project_id, external_id, pipeline, caption, cta,"
        " hashtags, media_ids, scheduled_for) VALUES ('deploysafe', %s, 'story',"
        " '{\"default\": \"you minified your code.\"}',"
        " '{\"tiktok\": \"scan at deploysafe.io\"}',"
        " '{\"tiktok\": [\"#vibecoding\", \"#nocode\"]}', '{}', now())"
        " RETURNING id",
        (f"post-{name}",),
    ).fetchone()["id"]
    target = conn.execute(
        "INSERT INTO targets (post_id, account_id, surface, state)"
        " VALUES (%s, %s, 'post', %s) RETURNING id", (post, account, state),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO attempts (target_id, n, idem_key, request, finished_at)"
        f" VALUES (%s, 1, %s, '{{}}', {finished})", (target, f"target:{target}:1"),
    )
    conn.commit()
    return target


def test_a_delivered_draft_gets_its_exact_caption(conn):
    target = delivered(conn)
    recorder = Recorder()

    sent, failures = notify_drafts(conn, recorder)

    assert (sent, failures) == (1, [])
    subject, text, key = recorder.sent[0]
    # The body is the caption and nothing else, so it can be copied whole.
    assert text == ("you minified your code.\n\nscan at deploysafe.io\n\n"
                    "#vibecoding #nocode")
    assert "@johnsecurity" in subject
    assert key == f"draft-notice/{target}"


def test_it_is_sent_once(conn):
    delivered(conn)
    recorder = Recorder()

    notify_drafts(conn, recorder)
    notify_drafts(conn, recorder)

    assert len(recorder.sent) == 1


def test_a_failed_email_is_retried_on_the_next_run(conn):
    delivered(conn)

    sent, failures = notify_drafts(conn, Recorder(fail=True))
    assert sent == 0 and len(failures) == 1

    recorder = Recorder()
    assert notify_drafts(conn, recorder)[0] == 1
    assert len(recorder.sent) == 1


def test_a_live_account_gets_no_email(conn):
    """Instagram posts directly, caption included. Nothing to paste."""
    delivered(conn, draft=False, name="deployedunsafe")
    recorder = Recorder()

    notify_drafts(conn, recorder)

    assert recorder.sent == []


def test_an_undelivered_draft_gets_no_email(conn):
    delivered(conn, state="pending")
    recorder = Recorder()

    notify_drafts(conn, recorder)

    assert recorder.sent == []


def test_old_drafts_are_not_emailed_when_an_account_turns_draft(conn):
    delivered(conn, finished="now() - interval '10 days'")
    recorder = Recorder()

    notify_drafts(conn, recorder)

    assert recorder.sent == []


def test_a_broken_notifier_does_not_fail_the_worker_run(conn):
    """The posts are already out. An email outage is not a failed run, and
    must never send a target back for a second publish."""
    target = delivered(conn)

    class Exploding(Recorder):
        def send(self, *a, **k):
            raise RuntimeError("not a NotifyError")

    stats = Worker(conn, lambda account: None, notifier=Exploding()).run_once()

    assert stats.errors == []
    state = conn.execute("SELECT state, attempts FROM targets WHERE id = %s",
                         (target,)).fetchone()
    assert (state["state"], state["attempts"]) == ("published", 0)


def test_no_notifier_means_no_emails_and_no_error(conn):
    delivered(conn)

    stats = Worker(conn, lambda account: None).run_once()

    assert (stats.notified, stats.errors) == (0, [])


def test_unset_key_turns_the_notifier_off(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setattr("socialq.notify.load_dotenv", lambda: None)

    assert Notifier.from_env() is None


def test_the_request_resend_receives():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["auth"] = request.headers["authorization"]
        seen["idem"] = request.headers["idempotency-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x"})

    notifier = Notifier("re_key", "drafts@notify.socialq.me", "me@example.com",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    notifier.send("subject", "the caption", idempotency_key="draft-notice/7")

    assert seen["auth"] == "Bearer re_key"
    assert seen["idem"] == "draft-notice/7"
    assert seen["body"] == {"from": "drafts@notify.socialq.me",
                            "to": ["me@example.com"], "subject": "subject",
                            "text": "the caption"}


def test_a_rejected_send_raises_so_the_row_is_not_written():
    notifier = Notifier("k", "a@b.c", "d@e.f", client=httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(403, json={"message": "domain not verified"}))))

    with pytest.raises(NotifyError, match="403"):
        notifier.send("s", "t", idempotency_key="k")


def test_a_long_caption_makes_a_short_subject():
    assert len(subject_for("@a", "tiktok", "x" * 300)) < 100
