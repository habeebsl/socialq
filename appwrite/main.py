"""Appwrite Functions entrypoint. §11.

Appwrite supplies a scheduler and an environment; nothing else. Each function
is this same file with a different COMMAND variable and a different schedule,
so the runtime knows nothing about publishing and the queue knows nothing about
Appwrite.

    worker     * * * * *      claim due work, publish, record   (§6)
    reconcile  */10 * * * *   resolve in_flight, refresh tokens (§7, §8.3)
    prune      0 4 * * *      delete published media from R2    (§9)

Deploy with scripts/deploy_appwrite.sh, which assembles this file plus the
socialq package into an upload directory -- Appwrite uploads a folder, and the
package has to travel with the entrypoint.
"""

import os
import sys

# Appwrite imports this file without putting its directory on sys.path, so the
# socialq package sitting next to it is invisible until we say otherwise.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main(context):
    command = os.environ.get("COMMAND", "status")

    # Scheduled executions are asynchronous and get the full function timeout;
    # a synchronous trigger is capped at 30 seconds by Appwrite regardless of
    # what the function is configured for. Publishing a video can exceed that,
    # so a manual test should be run with async=true.
    context.log(f"socialq {command}")

    from socialq.cli import main as cli

    try:
        code = cli([command])
    except Exception as exc:  # noqa: BLE001 -- the log is the only place a
        # scheduled failure surfaces, so it must carry the reason.
        context.error(f"socialq {command} raised {type(exc).__name__}: {exc}")
        raise

    if code:
        # A non-zero exit means something needs attention. Appwrite marks the
        # execution failed, which is what makes it visible in the console.
        context.error(f"socialq {command} exited {code}")
        return context.res.json({"ok": False, "command": command, "code": code},
                                500)

    return context.res.json({"ok": True, "command": command})


if __name__ == "__main__":  # local check: python appwrite/main.py worker
    from socialq.cli import main as cli

    sys.exit(cli([sys.argv[1] if len(sys.argv) > 1 else "status"]))
