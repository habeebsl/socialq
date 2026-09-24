"""Command line entry points.

The runtime is a scheduler that runs these commands; nothing about the queue
cares which one. §11 chose Modal, but that choice lives in modal_app.py and in
this file's absence from it -- Railway, Render, Fly, a systemd timer or plain
cron all work by calling:

    python -m socialq worker        # every minute
    python -m socialq reconcile     # every ten minutes
    python -m socialq prune         # daily
    python -m socialq migrate       # after a schema change
    python -m socialq status        # what is in the queue right now

Exit code is non-zero when something needs attention, so a scheduler that
reports failures reports the right things.
"""

from __future__ import annotations

import argparse
import logging
import sys


def _setup(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs the full request URL at INFO, and Instagram takes its access
    # token as a query parameter -- so this would write a live 60-day token
    # into every execution log the runtime keeps. Never raise this above
    # WARNING, including under --verbose.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _store():
    """The secret store.

    Postgres-backed whenever an encryption key is configured, which is the
    production case on every runtime: §8.3's token refresh must be able to
    write, and the environment cannot. Falls back to the environment for local
    one-off commands, where a read-only store is honest about what it can do.
    """
    import os

    from .config import load_dotenv

    if not os.environ.get("SOCIALQ_SECRET_KEY"):
        load_dotenv()
    if os.environ.get("SOCIALQ_SECRET_KEY"):
        from .secrets import PostgresSecretStore

        return PostgresSecretStore()

    from .secrets import EnvSecretStore

    return EnvSecretStore()


def cmd_worker(args) -> int:
    from .db import connect
    from .registry import Registry
    from .worker import Worker

    with connect() as conn:
        stats = Worker(conn, Registry(conn, _store()), batch=args.batch).run_once()

    if stats.claimed:
        print(f"claimed={stats.claimed} published={stats.published} "
              f"retried={stats.retried} waiting={stats.waiting} dead={stats.dead}")
    for error in stats.errors:
        print(f"ERROR {error}", file=sys.stderr)
    return 1 if (stats.errors or stats.dead) else 0


def cmd_reconcile(args) -> int:
    from .db import connect
    from .reconcile import Reconciler, refresh_tokens
    from .registry import Registry

    store = _store()
    with connect() as conn:
        # Keep secret-store entries alive. On Modal a Dict entry expires after
        # 7 days of inactivity and a quiet fortnight would lose the Instagram
        # token outright. Harmless everywhere else.
        touch = getattr(store, "touch", None)
        if touch:
            refs = [row["api_key_ref"] for row in conn.execute(
                "SELECT api_key_ref FROM publisher_credentials WHERE enabled"
            ).fetchall()]
            touch(refs)

        registry = Registry(conn, store)
        stats = Reconciler(conn, registry).run_once()
        refreshed = refresh_tokens(
            conn, lambda row: (registry.for_credential_row(row), store)
        )

    if stats.checked or stats.released or refreshed:
        print(f"checked={stats.checked} recovered={stats.recovered} "
              f"requeued={stats.requeued} released={stats.released} "
              f"dead={stats.dead} refreshed={refreshed}")
    for error in stats.errors:
        print(f"ERROR {error}", file=sys.stderr)
    return 1 if stats.errors else 0


def cmd_prune(args) -> int:
    from .config import R2Config
    from .db import connect
    from .media import MediaStore
    from .prune import prune, usage

    store = MediaStore(R2Config.from_env())
    with connect() as conn:
        stats = prune(conn, store, dry_run=args.dry_run)
        remaining = usage(conn)

    print(f"pruned={stats.pruned} (orphans={stats.orphans}) "
          f"freed={stats.bytes_freed / 1e9:.2f}GB "
          f"remaining={remaining['bytes'] / 1e9:.2f}GB "
          f"({remaining['objects']} objects)")
    if remaining["bytes"] > 7e9:                      # 10 GB free tier (§9)
        print(f"ALERT R2 at {remaining['bytes'] / 1e9:.1f}GB of 10GB",
              file=sys.stderr)
    for error in stats.errors:
        print(f"ERROR {error}", file=sys.stderr)
    return 1 if stats.errors else 0


def cmd_migrate(args) -> int:
    from .migrate import migrate

    applied = migrate()
    print(f"applied: {applied or 'nothing, already up to date'}")
    return 0


def cmd_status(args) -> int:
    """What the queue looks like. The first thing to run when something is off."""
    from .db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT state, count(*) AS n FROM targets GROUP BY state ORDER BY state"
        ).fetchall()
        print("targets:", ", ".join(f"{r['state']}={r['n']}" for r in rows) or "none")

        due = conn.execute(
            "SELECT count(*) AS n FROM targets t JOIN posts p ON p.id = t.post_id"
            " JOIN accounts a ON a.id = t.account_id WHERE t.state = 'pending'"
            " AND a.enabled AND p.scheduled_for <= now()"
            " AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())"
        ).fetchone()["n"]
        print(f"due now: {due}")

        for row in conn.execute(
            "SELECT handle, platform, publisher, enabled FROM accounts ORDER BY id"
        ).fetchall():
            state = "enabled" if row["enabled"] else "HELD"
            print(f"  {row['handle']:<20} {row['platform']:<10} "
                  f"{row['publisher']:<16} {state}")

        for row in conn.execute(
            "SELECT label, publisher, expires_at, credits_left, enabled"
            " FROM publisher_credentials ORDER BY id"
        ).fetchall():
            bits = [row["publisher"], row["label"]]
            if row["expires_at"]:
                bits.append(f"expires {row['expires_at']:%Y-%m-%d}")
            if row["credits_left"] is not None:
                bits.append(f"{row['credits_left']} credits")
            if not row["enabled"]:
                bits.append("DISABLED")
            print("  " + "  ".join(str(b) for b in bits))

        dead = conn.execute(
            "SELECT id, last_error FROM targets WHERE state = 'dead'"
            " ORDER BY id DESC LIMIT 5"
        ).fetchall()
        for row in dead:
            print(f"  DEAD target {row['id']}: {(row['last_error'] or '')[:80]}")
    return 0


def cmd_doctor(args) -> int:
    """Check everything a publish depends on, without publishing.

    Run it after any deploy. The failures it catches -- an unreadable secret,
    an expired token, R2 credentials that were never set -- otherwise surface
    as a post that silently did not go out (§14).
    """
    from datetime import datetime, timezone

    from .db import connect
    from .migrate import pending
    from .secrets import MissingSecret

    problems = []

    def check(name, fn):
        try:
            print(f"  {name}: {fn()}")
        except Exception as exc:  # noqa: BLE001 -- reporting is the job
            print(f"  {name}: FAIL {type(exc).__name__}: {exc}")
            problems.append(name)

    store = _store()
    print(f"secret store: {type(store).__name__}")

    with connect() as conn:
        print("database:")
        check("connection", lambda: conn.execute(
            "SELECT version()").fetchone()["version"].split(",")[0])
        check("migrations", lambda: (
            "up to date" if not pending(conn)
            else f"{len(pending(conn))} PENDING"))

        rows = conn.execute(
            "SELECT id, project_id, publisher, label, api_key_ref, account_ids,"
            " expires_at FROM publisher_credentials WHERE enabled ORDER BY id"
        ).fetchall()
        print(f"credentials ({len(rows)}):")
        if not rows:
            problems.append("no credentials")
            print("  none registered -- nothing can publish")

        for row in rows:
            label = f"{row['publisher']}/{row['label']}"
            try:
                secret = store.get(row["api_key_ref"])
                print(f"  {label}: secret readable ({len(secret)} chars)")
            except (MissingSecret, Exception) as exc:  # noqa: B014
                print(f"  {label}: SECRET UNREADABLE {exc}")
                problems.append(label)
                continue

            if row["expires_at"]:
                days = (row["expires_at"] - datetime.now(timezone.utc)).days
                state = "OK" if days > 14 else "REFRESH DUE"
                print(f"  {label}: expires in {days} days [{state}]")
                if days < 0:
                    problems.append(f"{label} expired")

            if row["publisher"] == "instagram_graph":
                from .publishers.instagram import InstagramGraph

                ig_id = (row["account_ids"] or {}).get("instagram")
                check(f"  {label} live", lambda s=secret, i=ig_id: InstagramGraph(
                    s, str(i))._call("GET", "me", fields="username")["username"])

        accounts = conn.execute(
            "SELECT a.id, a.handle, a.platform, a.enabled,"
            " (SELECT count(*) FROM publisher_account_ids m"
            "  WHERE m.account_id = a.id) AS pairings"
            " FROM accounts a ORDER BY a.id"
        ).fetchall()
        print(f"accounts ({len(accounts)}):")
        for account in accounts:
            state = "enabled" if account["enabled"] else "HELD"
            # §8.2.1: an account with no pairing cannot be published to at all,
            # and one with fewer than the credential count cannot use the whole
            # rotation -- both are invisible until a post fails.
            pairings = account["pairings"]
            note = "" if pairings else "  NO CREDENTIAL PAIRED"
            if not pairings:
                problems.append(f"{account['handle']} unpaired")
            print(f"  {account['handle']} {account['platform']} [{state}] "
                  f"{pairings} credential(s){note}")

        # Resolve each account the way the worker does -- through Registry,
        # the same call publish_one() makes. Anything else would prove the
        # code exists, not that it is the code being run.
        print("resolution (the worker's own path):")
        from .models import Account
        from .registry import Registry

        registry = Registry(conn, store)
        for row in accounts:
            if not row["enabled"]:
                print(f"  {row['handle']}: held, not resolved")
                continue
            full = conn.execute(
                "SELECT id, project_id, name, platform, handle, publisher,"
                " external_id, enabled FROM accounts WHERE id = %s", (row["id"],)
            ).fetchone()
            try:
                built = registry(Account(**full))
            except Exception as exc:  # noqa: BLE001
                print(f"  {row['handle']}: FAIL {type(exc).__name__}: {exc}")
                problems.append(f"{row['handle']} unresolvable")
                continue
            detail = getattr(built, "ig_user_id", None) or getattr(
                built, "account_id", "?")
            credential_id = getattr(built, "credential_id", None)
            print(f"  {row['handle']}: {built.name} -> id {detail}"
                  + (f" via credential {credential_id}" if credential_id else ""))

    print("storage:")
    try:
        from .config import R2Config
        from .media import MediaStore

        media_store = MediaStore(R2Config.from_env())
        media_store.client.head_bucket(Bucket=media_store.config.bucket)
        print(f"  r2: {media_store.config.bucket} reachable")
    except Exception as exc:  # noqa: BLE001
        print(f"  r2: FAIL {type(exc).__name__}: {exc}")
        problems.append("r2")

    print()
    if problems:
        print(f"PROBLEMS: {', '.join(problems)}")
        return 1
    print("all checks passed")
    return 0


def cmd_keygen(args) -> int:
    """A new encryption key for the Postgres secret store.

    Keep it in the runtime's own secret manager, never in the database it
    protects -- that is the entire point of it being separate.
    """
    from .secrets import generate_key

    print(generate_key())
    return 0


def cmd_secrets(args) -> int:
    """Move secrets from the environment into the encrypted store.

    Run once per deploy target. After this the environment no longer needs the
    tokens, and a refreshed one (§8.3) has somewhere durable to land.
    """
    import os

    from .config import load_dotenv
    from .db import connect
    from .secrets import MissingSecret, PostgresSecretStore

    load_dotenv()
    store = PostgresSecretStore()

    if args.push:
        with connect() as conn:
            refs = [row["api_key_ref"] for row in conn.execute(
                "SELECT DISTINCT api_key_ref FROM publisher_credentials"
            ).fetchall()]
        if not refs:
            print("no credentials registered yet; nothing to push")
            return 1
        for ref in refs:
            value = os.environ.get(ref)
            if not value:
                print(f"  {ref}: NOT in the environment, skipped")
                continue
            store.set(ref, value)
            print(f"  {ref}: stored ({len(value)} chars)")

    stored = store.refs()
    print(f"secrets in store: {', '.join(stored) if stored else 'none'}")

    # Prove they decrypt: an unreadable secret is worse than a missing one,
    # because nothing notices until a publish fails.
    for ref in stored:
        try:
            store.get(ref)
        except MissingSecret as exc:
            print(f"  {ref}: UNREADABLE {exc}")
            return 1
    return 0


COMMANDS = {
    "worker": cmd_worker,
    "reconcile": cmd_reconcile,
    "prune": cmd_prune,
    "migrate": cmd_migrate,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "keygen": cmd_keygen,
    "secrets": cmd_secrets,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="socialq")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    worker = sub.add_parser("worker", help="claim due work and publish it")
    worker.add_argument("--batch", type=int, default=10)
    sub.add_parser("reconcile", help="resolve in_flight targets, refresh tokens")
    prune = sub.add_parser("prune", help="delete published media from R2")
    prune.add_argument("--dry-run", action="store_true")
    sub.add_parser("migrate", help="apply pending migrations")
    sub.add_parser("status", help="what is in the queue")
    sub.add_parser("doctor", help="check everything a publish depends on")
    sub.add_parser("keygen", help="generate a SOCIALQ_SECRET_KEY")
    secrets = sub.add_parser("secrets", help="inspect or seed the secret store")
    secrets.add_argument("--push", action="store_true",
                         help="copy credential secrets from the environment in")

    args = parser.parse_args(argv)
    _setup(args.verbose)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
