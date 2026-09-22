"""Modal deployment. §11.

Python-native, scale-to-zero, no server to keep alive, and the free tier covers
this volume. Rendering stays in video-maker and never runs here, so the image
is small -- no torch, no ffmpeg.

    modal deploy modal_app.py

GitHub Actions is the wrong tool for this: 5-minute minimum granularity,
frequent queue delays, and nowhere for a reconciliation loop to live.
"""

from __future__ import annotations

import modal

APP_NAME = "socialq"
SECRET_NAME = "socialq"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("psycopg[binary]>=3.2", "httpx>=0.27", "boto3>=1.34")
    .add_local_python_source("socialq")
)

app = modal.App(APP_NAME, image=image)

secrets = [modal.Secret.from_name(SECRET_NAME)]


def _run(command: str) -> int:
    """Every job is the CLI. Modal supplies a scheduler and an environment and
    nothing else, which is what keeps §11's choice reversible: the same
    commands run under Railway, cron or a systemd timer."""
    from socialq.cli import main

    return main([command])


@app.function(schedule=modal.Period(minutes=1), secrets=secrets, timeout=600)
def worker():
    """Claim due work, publish it, record it. §6."""
    _run("worker")


@app.function(schedule=modal.Period(minutes=10), secrets=secrets, timeout=900)
def reconcile():
    """§7, plus token refresh (§8.3) and keeping the secret Dict alive."""
    _run("reconcile")


@app.function(schedule=modal.Cron("0 4 * * *"), secrets=secrets, timeout=900)
def prune_media():
    """§9: prune published media rather than discovering the ceiling at 90%."""
    _run("prune")


@app.function(secrets=secrets, timeout=600)
def migrate():
    """Apply pending migrations:  modal run modal_app.py::migrate"""
    _run("migrate")


@app.function(secrets=secrets, timeout=300)
def status():
    """What is in the queue:  modal run modal_app.py::status"""
    _run("status")


@app.local_entrypoint()
def main():
    """`modal run modal_app.py` -- one pass of each, for a smoke test."""
    status.remote()
    worker.remote()
    reconcile.remote()
