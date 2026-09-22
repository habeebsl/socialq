"""One real video to Instagram, through the whole pipeline. §12 step 3, §13.

Enqueues through the SDK and runs one worker pass, so this exercises what
production will: R2 upload, media row, post row, target, claim, container,
polling, publish, record.

    python scripts/smoke_instagram.py --media path/to/video.mp4

Free -- Instagram costs no credits. But it IS public the moment it lands, and
the Instagram-Login route has no delete endpoint, so removing it afterwards is
a manual step in the app.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from socialq import Client
from socialq.config import load_dotenv
from socialq.db import connect
from socialq.registry import Registry
from socialq.secrets import EnvSecretStore
from socialq.worker import Worker

TOKEN_REF = "INSTAGRAM_ACCESS_TOKEN"


def ensure_account(conn, project: str, handle: str, ig_user_id: str) -> None:
    conn.execute(
        "INSERT INTO projects (id) VALUES (%s) ON CONFLICT DO NOTHING", (project,)
    )
    conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher,"
        " external_id) VALUES (%s,%s,'instagram',%s,'instagram_graph',%s)"
        " ON CONFLICT (project_id, name, platform) DO UPDATE"
        " SET enabled = true, external_id = EXCLUDED.external_id",
        (project, handle.lstrip("@"), handle, ig_user_id),
    )
    conn.execute(
        "INSERT INTO publisher_credentials (project_id, publisher, label,"
        " api_key_ref, account_ids) VALUES (%s,'instagram_graph','main',%s,%s)"
        " ON CONFLICT (project_id, publisher, label) DO UPDATE"
        " SET api_key_ref = EXCLUDED.api_key_ref,"
        "     account_ids = EXCLUDED.account_ids, enabled = true",
        (project, TOKEN_REF, json.dumps({"instagram": ig_user_id})),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", required=True)
    parser.add_argument("--project", default="deploysafe")
    parser.add_argument("--handle", default="@pro.mptandpray")
    parser.add_argument("--caption", required=True,
                        help="this is published and cannot be edited by the API")
    parser.add_argument("--cta", default="")
    parser.add_argument("--hashtags", default="",
                        help="space separated, e.g. '#vibecoding #buildinpublic'")
    parser.add_argument("--surface", default="post", choices=("post", "story"))
    parser.add_argument("--external-id", default="smoke-ig-1")
    parser.add_argument("--aigc", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    media = Path(args.media)
    if not media.is_file():
        print(f"no such file: {media}")
        return 1

    secrets = EnvSecretStore()
    ig_user_id = secrets.get("INSTAGRAM_USER_ID")

    caption = {"default": args.caption}
    cta = {"instagram": args.cta} if args.cta else {}
    hashtags = {"instagram": args.hashtags.split()} if args.hashtags else {}

    print(f"posting {media.name} to {args.handle} as a {args.surface}")
    print(f"aigc: {args.aigc}\n")

    sq = Client(base_dir=media.parent)
    with connect() as conn:
        ensure_account(conn, args.project, args.handle, ig_user_id)

    result = sq.enqueue(
        project=args.project,
        pipeline="smoke",
        external_id=args.external_id,
        media=[media.name],
        caption=caption,
        cta=cta,
        hashtags=hashtags,
        surface={"instagram": args.surface},
        accounts={"instagram": [args.handle.lstrip("@")]},
        aigc=args.aigc,
    )
    print(f"enqueued post {result.post_id}, targets {result.targets}")
    if not result.created:
        print("(already enqueued -- pass a new --external-id to post again)")

    with connect() as conn:
        registry = Registry(conn, secrets)
        stats = Worker(conn, registry, worker_id="smoke-ig", batch=5).run_once()
        print(f"\nclaimed={stats.claimed} published={stats.published} "
              f"retried={stats.retried} waiting={stats.waiting} dead={stats.dead}")
        for error in stats.errors:
            print(f"  error: {error}")

        for row in conn.execute(
            "SELECT id, state, provider_post_id, permalink, last_error"
            " FROM targets WHERE post_id = %s", (result.post_id,)
        ).fetchall():
            print(f"\ntarget {row['id']}: {row['state']}")
            for field in ("provider_post_id", "permalink", "last_error"):
                if row[field]:
                    print(f"  {field:<17}{row[field]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
