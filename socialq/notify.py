"""Caption emails for drafts.

A draft is finished by a person in the app, and TikTok's video draft endpoint
carries no caption at all, so the text has to reach that person some other
way. The worker emails it: plain text, nothing but the caption, so select-all
and copy is the whole job.

Sending is a sweep over delivered drafts with no `draft_notices` row, not a
step inside publish. That keeps the two failures apart: an email that fails is
retried on the next run, and can never cause the post itself to be retried --
which would put a second draft in the inbox.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx
import psycopg

from . import repo
from .config import load_dotenv
from .text import text_for

log = logging.getLogger("socialq.notify")

RESEND_URL = "https://api.resend.com/emails"

# Only drafts delivered recently. Without a bound, switching an old account to
# draft would email the caption of everything it ever published.
RECENT = "2 days"

OWED_SQL = f"""
SELECT t.id, t.post_id, a.platform, a.handle
FROM targets t
JOIN accounts a ON a.id = t.account_id
WHERE t.state = 'published'
  AND a.draft
  AND NOT EXISTS (SELECT 1 FROM draft_notices n WHERE n.target_id = t.id)
  AND EXISTS (
    SELECT 1 FROM attempts x
    WHERE x.target_id = t.id AND x.finished_at > now() - interval '{RECENT}'
  )
ORDER BY t.id
LIMIT %(limit)s
"""


class NotifyError(RuntimeError):
    pass


@dataclass
class Notifier:
    """Sends one plain-text email through Resend."""

    api_key: str
    sender: str
    recipient: str
    client: httpx.Client | None = None

    @classmethod
    def from_env(cls) -> "Notifier | None":
        """None when not configured: the emails are optional, the posts are not."""
        load_dotenv()
        key = os.environ.get("RESEND_API_KEY")
        sender = os.environ.get("NOTIFY_EMAIL_FROM")
        recipient = os.environ.get("NOTIFY_EMAIL_TO")
        if not (key and sender and recipient):
            return None
        return cls(key, sender, recipient)

    def send(self, subject: str, text: str, *, idempotency_key: str) -> None:
        client = self.client or httpx.Client(timeout=20.0)
        try:
            response = client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    # A crash between the send and the row being written must
                    # not become a second email on the next run.
                    "Idempotency-Key": idempotency_key,
                },
                json={
                    "from": self.sender,
                    "to": [self.recipient],
                    "subject": subject,
                    "text": text,
                },
            )
        except httpx.RequestError as exc:
            raise NotifyError(f"resend: {exc}") from exc
        if response.status_code >= 300:
            raise NotifyError(
                f"resend: HTTP {response.status_code} {response.text[:200]}"
            )


def subject_for(handle: str, platform: str, text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    if len(first) > 60:
        first = first[:57].rstrip() + "..."
    return f"{platform} draft ready for {handle}: {first}"


def notify_drafts(
    conn: psycopg.Connection, notifier: Notifier, *, limit: int = 10
) -> tuple[int, list[str]]:
    """Email the caption for each delivered draft that has not had one.

    Returns (sent, failures). Never raises for a failed send: the row is not
    written, so the next run tries again.
    """
    owed = conn.execute(OWED_SQL, {"limit": limit}).fetchall()
    sent, failures = 0, []
    for row in owed:
        post = repo.load_post(conn, row["post_id"])
        text = text_for(post, row["platform"])
        try:
            notifier.send(
                subject_for(row["handle"], row["platform"], text),
                # The caption and nothing else, so it can be copied whole.
                text,
                idempotency_key=f"draft-notice/{row['id']}",
            )
        except NotifyError as exc:
            log.warning("target %s: caption email failed: %s", row["id"], exc)
            failures.append(f"target {row['id']}: {exc}")
            continue
        conn.execute(
            "INSERT INTO draft_notices (target_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (row["id"],),
        )
        conn.commit()
        sent += 1
        log.info("target %s: caption emailed to %s", row["id"], notifier.recipient)
    return sent, failures
