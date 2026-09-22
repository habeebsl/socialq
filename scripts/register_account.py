"""Register an account and its credential. §8.4, §14.

Verifies the credential against the platform before writing anything, so a
typo fails here rather than six hours later in the worker.

    python scripts/register_account.py --platform instagram \
        --handle @deployedunsafe --token-ref INSTAGRAM_ACCESS_TOKEN

§14: new accounts are registered DISABLED and warmed by hand for about a week.
A brand new account that starts posting on a schedule is the shape platforms
throttle. Pass --enable only once it is warm.
"""

from __future__ import annotations

import argparse
import json
import sys

from socialq.config import load_dotenv
from socialq.db import connect
from socialq.publishers.instagram import InstagramGraph
from socialq.publishers.postpeer import PostPeerTikTok
from socialq.secrets import EnvSecretStore

PUBLISHER_FOR = {"instagram": "instagram_graph", "tiktok": "postpeer"}


def verify_instagram(secrets, token_ref: str, user_id_ref: str):
    """Confirm the token works and the account can publish."""
    token = secrets.get(token_ref)
    user_id = secrets.get(user_id_ref)
    ig = InstagramGraph(token, user_id)
    me = ig._call("GET", "me", fields="id,username,account_type,media_count")

    account_type = me.get("account_type")
    if account_type not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
        raise SystemExit(
            f"account_type is {account_type!r}; the Content Publishing API "
            "needs a Business or Creator account"
        )
    used, total = ig.publishing_limit()
    print(f"  verified @{me.get('username')} ({account_type}), "
          f"{me.get('media_count')} posts, quota {used}/{total}")
    return user_id, {"instagram": user_id}


def verify_postpeer(secrets, token_ref: str, platform: str):
    key = secrets.get(token_ref)
    client = PostPeerTikTok(key, account_id="")
    account_ids = {}
    for item in client.integrations():
        name = item.get("platform") or item.get("provider")
        account_id = str(item.get("accountId") or item.get("id") or "")
        if name and account_id:
            account_ids[name] = account_id
            print(f"  integration {name}: {account_id} "
                  f"{item.get('username') or ''}")
    if platform not in account_ids:
        raise SystemExit(
            f"this key has no {platform} integration; connect one in the "
            "PostPeer dashboard first"
        )
    return account_ids[platform], account_ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="deploysafe")
    parser.add_argument("--platform", required=True,
                        choices=sorted(PUBLISHER_FOR))
    parser.add_argument("--handle", required=True)
    parser.add_argument("--name", help="defaults to the handle without the @")
    parser.add_argument("--token-ref", required=True,
                        help="env var holding the secret, e.g. INSTAGRAM_ACCESS_TOKEN")
    parser.add_argument("--user-id-ref", default="INSTAGRAM_USER_ID")
    parser.add_argument("--label",
                        help="credential label; defaults to the account name. "
                             "Must be unique per publisher (§8.2.1) -- two "
                             "accounts sharing a label share a token.")
    parser.add_argument("--enable", action="store_true",
                        help="skip warming and go live immediately (§14)")
    args = parser.parse_args()

    load_dotenv()
    secrets = EnvSecretStore()
    publisher = PUBLISHER_FOR[args.platform]
    name = args.name or args.handle.lstrip("@")
    # One credential per account by default, so adding a second account cannot
    # silently overwrite the first one's token.
    label = args.label or name

    print(f"verifying {args.token_ref} against {args.platform}...")
    if publisher == "instagram_graph":
        external_id, account_ids = verify_instagram(
            secrets, args.token_ref, args.user_id_ref
        )
    else:
        external_id, account_ids = verify_postpeer(
            secrets, args.token_ref, args.platform
        )

    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id) VALUES (%s) ON CONFLICT DO NOTHING",
            (args.project,),
        )
        account = conn.execute(
            "INSERT INTO accounts (project_id, name, platform, handle, publisher,"
            " external_id, enabled) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (project_id, name, platform) DO UPDATE"
            " SET handle = EXCLUDED.handle, publisher = EXCLUDED.publisher,"
            "     external_id = EXCLUDED.external_id, enabled = EXCLUDED.enabled"
            " RETURNING id, enabled",
            (args.project, name, args.platform, args.handle, publisher,
             external_id, args.enable),
        ).fetchone()
        credential = conn.execute(
            "INSERT INTO publisher_credentials (project_id, publisher, label,"
            " api_key_ref, account_ids) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (project_id, publisher, label) DO UPDATE"
            " SET api_key_ref = EXCLUDED.api_key_ref,"
            "     account_ids = EXCLUDED.account_ids, enabled = true"
            " RETURNING id",
            (args.project, publisher, label, args.token_ref,
             json.dumps(account_ids)),
        ).fetchone()

        # Declare the pairing. §8.2.1: the id belongs to THIS credential for
        # THAT account -- a second PostPeer account holds a different one for
        # the same TikTok account, which is why it cannot live on either row
        # alone.
        conn.execute(
            "INSERT INTO publisher_account_ids (credential_id, account_id,"
            " external_id) VALUES (%s,%s,%s)"
            " ON CONFLICT (credential_id, account_id) DO UPDATE"
            " SET external_id = EXCLUDED.external_id",
            (credential["id"], account["id"], external_id),
        )

    state = "ENABLED -- it will be posted to" if account["enabled"] else (
        "disabled -- warm it by hand, then rerun with --enable (§14)")
    print(f"\nregistered account {account['id']}: {args.handle} on "
          f"{args.platform} via {publisher}")
    print(f"credential '{label}' -> {args.token_ref}")
    print(f"pairing: {label} posts to {args.handle} as {external_id}")
    print(f"status: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
