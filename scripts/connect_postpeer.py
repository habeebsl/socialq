"""Register a PostPeer credential and map its integrations to accounts. §8.2.1.

Rotation needs every PostPeer account connected to every TikTok account you
post to. Each one authorised TikTok separately, so each holds a DIFFERENT
integration id for the same account -- that pairing is what this writes.

    python scripts/connect_postpeer.py --label 1 --token-ref POSTPEER_API_KEY_1
    python scripts/connect_postpeer.py --coverage        # who can reach what

A TikTok account connected in only two of three PostPeer accounts does not have
60 credits available to it, it has 40 -- and you would otherwise discover that
when the rotation ran dry a third early. --coverage shows the gaps.
"""

from __future__ import annotations

import argparse
import json
import sys

from socialq.config import load_dotenv
from socialq.db import connect
from socialq.publishers.postpeer import PostPeerTikTok
from socialq.secrets import EnvSecretStore


def coverage(conn) -> int:
    """The (credential x account) grid, and its holes."""
    accounts = conn.execute(
        "SELECT id, name, handle, platform FROM accounts"
        " WHERE publisher = 'postpeer' ORDER BY id"
    ).fetchall()
    credentials = conn.execute(
        "SELECT id, label, credits_left, enabled FROM publisher_credentials"
        " WHERE publisher = 'postpeer' ORDER BY id"
    ).fetchall()

    if not accounts or not credentials:
        print("nothing registered for postpeer yet")
        return 0

    pairs = {
        (row["credential_id"], row["account_id"]): row["external_id"]
        for row in conn.execute(
            "SELECT credential_id, account_id, external_id"
            " FROM publisher_account_ids"
        ).fetchall()
    }

    width = max(len(a["handle"]) for a in accounts) + 2
    header = "".join(f"{c['label']:<14}" for c in credentials)
    print(f"{'account':<{width}}{header}")

    gaps = 0
    for account in accounts:
        cells = ""
        for credential in credentials:
            value = pairs.get((credential["id"], account["id"]))
            if value:
                cells += f"{value[:12]:<14}"
            else:
                cells += f"{'-- MISSING':<14}"
                gaps += 1
        print(f"{account['handle']:<{width}}{cells}")

    monthly = sum(20 for c in credentials if c["enabled"])
    print(f"\n{len(credentials)} credentials, nominally {monthly} credits/month")
    if gaps:
        print(f"{gaps} missing pairing(s): those accounts cannot use the whole "
              "rotation. Connect TikTok in that PostPeer dashboard, then rerun "
              "this script for that credential.")
        return 1
    print("every account is reachable through every credential")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="deploysafe")
    parser.add_argument("--label", help="which PostPeer account, e.g. 1, 2, 3")
    parser.add_argument("--token-ref", help="env var holding that account's key")
    parser.add_argument("--coverage", action="store_true",
                        help="show the grid and exit")
    args = parser.parse_args()

    load_dotenv()

    if args.coverage:
        with connect() as conn:
            return coverage(conn)

    if not (args.label and args.token_ref):
        parser.error("--label and --token-ref are required unless --coverage")

    key = EnvSecretStore().get(args.token_ref)
    client = PostPeerTikTok(key, account_id="")
    integrations = client.integrations()
    if not integrations:
        print(f"{args.token_ref} has no connected integrations; connect the "
              "TikTok account in that PostPeer dashboard first")
        return 1

    print(f"integrations on {args.token_ref}:")
    found = {}
    for item in integrations:
        platform = item.get("platform") or item.get("provider") or ""
        external_id = str(item.get("accountId") or item.get("id") or "")
        handle = (item.get("username") or item.get("handle")
                  or item.get("name") or "")
        print(f"  {platform:<10} {external_id:<28} {handle}")
        if platform and external_id:
            found[(platform, handle.lstrip("@").lower())] = external_id

    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id) VALUES (%s) ON CONFLICT DO NOTHING",
            (args.project,),
        )
        credential = conn.execute(
            "INSERT INTO publisher_credentials (project_id, publisher, label,"
            " api_key_ref, account_ids) VALUES (%s,'postpeer',%s,%s,%s)"
            " ON CONFLICT (project_id, publisher, label) DO UPDATE"
            " SET api_key_ref = EXCLUDED.api_key_ref, enabled = true"
            " RETURNING id",
            (args.project, args.label, args.token_ref,
             json.dumps({p: i for (p, _), i in found.items()})),
        ).fetchone()

        accounts = conn.execute(
            "SELECT id, name, handle, platform FROM accounts"
            " WHERE project_id = %s AND publisher = 'postpeer'",
            (args.project,),
        ).fetchall()

        linked, unmatched = 0, []
        for account in accounts:
            handle = account["handle"].lstrip("@").lower()
            external_id = (found.get((account["platform"], handle))
                           or found.get((account["platform"],
                                         account["name"].lower())))
            if not external_id:
                unmatched.append(account["handle"])
                continue
            conn.execute(
                "INSERT INTO publisher_account_ids (credential_id, account_id,"
                " external_id) VALUES (%s,%s,%s)"
                " ON CONFLICT (credential_id, account_id) DO UPDATE"
                " SET external_id = EXCLUDED.external_id",
                (credential["id"], account["id"], external_id),
            )
            linked += 1
            print(f"\n  paired {account['handle']} -> {external_id}")

        for handle in unmatched:
            print(f"  {handle}: NOT connected in this PostPeer account")

        if not accounts:
            print("\nno postpeer accounts registered yet; register the TikTok "
                  "account first with scripts/register_account.py")

        print()
        coverage(conn)

    return 0 if linked or not accounts else 1


if __name__ == "__main__":
    sys.exit(main())
