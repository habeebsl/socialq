"""One real TikTok post, through the whole pipeline. §12 step 4.

Enqueues through the SDK and runs one worker pass, so this exercises exactly
what production will: R2 upload, media row, post row, target, claim, publish,
record.

    python scripts/smoke_tiktok.py --media path/to/video.mp4            # draft
    python scripts/smoke_tiktok.py --media path/to/video.mp4 --live     # public

COSTS ONE CREDIT either way -- a draft is still a post as far as PostPeer is
concerned. The free tier is 20 a month.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from socialq import Client
from socialq.config import load_dotenv
from socialq.db import connect
from socialq.registry import Registry
from socialq.secrets import EnvSecretStore
from socialq.worker import Worker


def ensure_account(conn, project: str, handle: str) -> int:
    conn.execute(
        "INSERT INTO projects (id) VALUES (%s) ON CONFLICT DO NOTHING", (project,)
    )
    row = conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher)"
        " VALUES (%s, %s, 'tiktok', %s, 'postpeer')"
        " ON CONFLICT (project_id, name, platform) DO UPDATE SET enabled = true"
        " RETURNING id",
        (project, handle.lstrip("@"), handle),
    ).fetchone()
    return row["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", required=True, help="a real video file")
    parser.add_argument("--project", default="deploysafe")
    parser.add_argument("--handle", default="@deployedunsafe")
    parser.add_argument("--caption", default="socialq pipeline test")
    parser.add_argument("--external-id", default="smoke-1",
                        help="change this to post again: enqueue is idempotent")
    parser.add_argument("--live", action="store_true",
                        help="publish publicly instead of to drafts")
    parser.add_argument("--aigc", action="store_true",
                        help="disclose as AI-generated")
    args = parser.parse_args()

    load_dotenv()
    media = Path(args.media)
    if not media.is_file():
        print(f"no such file: {media}")
        return 1

    mode = "LIVE (public)" if args.live else "draft"
    print(f"posting {media.name} to {args.handle} as {mode}")
    print("this costs 1 PostPeer credit.\n")

    sq = Client(base_dir=media.parent)
    with connect() as conn:
        ensure_account(conn, args.project, args.handle)

    result = sq.enqueue(
        project=args.project,
        pipeline="smoke",
        external_id=args.external_id,
        media=[media.name],
        caption={"default": args.caption},
        surface={"tiktok": "post"},
        accounts={"tiktok": [args.handle.lstrip("@")]},
        aigc=args.aigc,
    )
    print(f"enqueued post {result.post_id}, targets {result.targets}")
    if not result.created:
        print("(already enqueued -- pass a new --external-id to post again)")

    with connect() as conn:
        registry = Registry(conn, EnvSecretStore(), draft=not args.live)
        stats = Worker(conn, registry, worker_id="smoke", batch=5).run_once()
        print(f"\nclaimed={stats.claimed} published={stats.published} "
              f"retried={stats.retried} waiting={stats.waiting} dead={stats.dead}")
        for error in stats.errors:
            print(f"  error: {error}")

        for row in conn.execute(
            "SELECT t.id, t.state, t.provider_post_id, t.permalink, t.last_error"
            " FROM targets t JOIN posts p ON p.id = t.post_id"
            " WHERE p.id = %s", (result.post_id,)
        ).fetchall():
            print(f"\ntarget {row['id']}: {row['state']}")
            if row["provider_post_id"]:
                print(f"  post id   {row['provider_post_id']}")
            if row["permalink"]:
                print(f"  permalink {row['permalink']}")
            if row["last_error"]:
                print(f"  error     {row['last_error']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
