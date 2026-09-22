"""Discover a PostPeer account's connected integrations. §8.2.1.

The credential is a pair: the API key and the accountId that key's own TikTok
integration was given. This prints the ids belonging to one key so the pairing
is never assembled by hand from two places.

    python scripts/postpeer_setup.py                 # list integrations (free)
    python scripts/postpeer_setup.py --usage         # also read credits (COSTS 1)
    python scripts/postpeer_setup.py --register a    # write the credential row

Reads POSTPEER_API_KEY_<label> from the environment or .env.
"""

from __future__ import annotations

import argparse
import json
import sys

from socialq.config import load_dotenv
from socialq.db import connect
from socialq.publishers.postpeer import PostPeerTikTok
from socialq.secrets import EnvSecretStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="1", help="which key, e.g. 1, 2, 3")
    parser.add_argument("--usage", action="store_true",
                        help="read the credit balance -- this costs one credit")
    parser.add_argument("--register", metavar="PROJECT",
                        help="insert the publisher_credentials row for a project")
    args = parser.parse_args()

    load_dotenv()
    ref = f"POSTPEER_API_KEY_{args.label}"
    try:
        api_key = EnvSecretStore().get(ref)
    except KeyError:
        print(f"{ref} is not set (see .env)")
        return 1

    client = PostPeerTikTok(api_key, account_id="")
    integrations = client.integrations()
    if not integrations:
        print("no integrations connected to this account.")
        print("connect one in the PostPeer dashboard first.")
        return 1

    account_ids: dict[str, str] = {}
    print(f"integrations on {ref}:\n")
    for item in integrations:
        platform = item.get("platform") or item.get("provider") or "?"
        account_id = str(item.get("accountId") or item.get("id") or "")
        handle = item.get("username") or item.get("handle") or item.get("name") or ""
        print(f"  {platform:<12} {account_id:<28} {handle}")
        if platform and account_id:
            account_ids[platform] = account_id

    if args.usage:
        print(f"\ncredits remaining: {client.credits_remaining()}  (this cost 1)")

    if args.register:
        with connect() as conn:
            conn.execute(
                "INSERT INTO projects (id) VALUES (%s) ON CONFLICT DO NOTHING",
                (args.register,),
            )
            conn.execute(
                "INSERT INTO publisher_credentials (project_id, publisher, label,"
                " api_key_ref, account_ids) VALUES (%s,'postpeer',%s,%s,%s)"
                " ON CONFLICT (project_id, publisher, label) DO UPDATE"
                " SET api_key_ref = EXCLUDED.api_key_ref,"
                "     account_ids = EXCLUDED.account_ids, enabled = true",
                (args.register, args.label, ref, json.dumps(account_ids)),
            )
        print(f"\nregistered credential '{args.label}' for project "
              f"{args.register} -> {account_ids}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
