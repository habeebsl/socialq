"""Per-account draft mode.

A draft account's posts land in the creator's drafts and never go public.
Per account, because one worker serves every account: a worker-wide switch
would turn the live Instagram account's posts into drafts too.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from socialq.registry import Registry, UnknownPublisher
from socialq.repo import load_account
from socialq.secrets import FileSecretStore

from test_postpeer import (
    connect_credential,
    make_post,
    seed_credentials,
    seed_tiktok_account,
    video,
)


@pytest.fixture
def store(tmp_path):
    secrets = FileSecretStore(tmp_path / "s.json")
    secrets.set("KEY_a", "secret-a")
    return secrets


def tiktok(conn, name, *, draft):
    """A TikTok account paired to credential 'a', loaded the way the worker
    loads it -- through the row, so the column is what is being tested."""
    account_id = seed_tiktok_account(conn, name)
    conn.execute("UPDATE accounts SET draft = %s WHERE id = %s",
                 (draft, account_id))
    connect_credential(conn, "a", account_id, f"tt_{name}")
    return load_account(conn, account_id)


def sent_draft_flag(publisher) -> bool:
    """What would actually go over the wire."""
    body = publisher.build_request(make_post(), [video()])
    return body["platforms"][0]["platformSpecificData"]["draft"]


def test_existing_accounts_are_not_drafts(conn, store):
    """The migration must not change what any live account does."""
    seed_credentials(conn, [("a", 9, True)])
    account = tiktok(conn, "live", draft=False)

    assert account.draft is False
    assert sent_draft_flag(Registry(conn, store)(account)) is False


def test_a_draft_account_sends_draft(conn, store):
    seed_credentials(conn, [("a", 9, True)])
    account = tiktok(conn, "johnsecurity", draft=True)

    assert sent_draft_flag(Registry(conn, store)(account)) is True


def test_one_worker_serves_a_draft_and_a_live_account_side_by_side(conn, store):
    """The reason it is per account: the same registry, two answers."""
    seed_credentials(conn, [("a", 9, True)])
    held = tiktok(conn, "johnsecurity", draft=True)
    live = tiktok(conn, "deployedunsafe", draft=False)
    registry = Registry(conn, store)

    assert sent_draft_flag(registry(held)) is True
    assert sent_draft_flag(registry(live)) is False


def test_the_worker_wide_flag_cannot_make_a_draft_account_public(conn, store):
    """Either flag forces a draft; neither can undo the other."""
    seed_credentials(conn, [("a", 9, True)])
    held = tiktok(conn, "johnsecurity", draft=True)
    live = tiktok(conn, "deployedunsafe", draft=False)

    assert sent_draft_flag(Registry(conn, store, draft=False)(held)) is True
    assert sent_draft_flag(Registry(conn, store, draft=True)(live)) is True


def test_a_draft_account_on_a_publisher_without_drafts_is_refused(conn, store):
    """The Graph API has no draft mode. Ignoring the flag would post publicly
    an account someone marked as must-not-post-publicly."""
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')")
    account_id = conn.execute(
        "INSERT INTO accounts (project_id, name, platform, handle, publisher,"
        " draft) VALUES ('deploysafe', 'ig', 'instagram', '@ig',"
        " 'instagram_graph', true) RETURNING id"
    ).fetchone()["id"]

    with pytest.raises(UnknownPublisher, match="no draft mode"):
        Registry(conn, store)(load_account(conn, account_id))


# -- registration: pairing by handle --------------------------------------


def register_script():
    path = Path(__file__).parent.parent / "scripts" / "register_account.py"
    spec = importlib.util.spec_from_file_location("register_account", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_integrations(monkeypatch, module, integrations):
    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        def integrations(self):
            return integrations

    monkeypatch.setattr(module, "PostPeerTikTok", Fake)


class Secrets:
    def get(self, ref):
        return "key"


def test_registration_pairs_the_named_handle_not_the_last_listed(monkeypatch):
    """One PostPeer key holding two TikTok accounts. The old code took
    whichever the API listed last -- here, the wrong one."""
    module = register_script()
    with_integrations(monkeypatch, module, [
        {"platform": "tiktok", "accountId": "tt_patch", "username": "patchjohn"},
        {"platform": "tiktok", "accountId": "tt_other", "username": "deployedunsafe"},
    ])

    external_id, _ = module.verify_postpeer(Secrets(), "K", "tiktok", "@patchjohn")

    assert external_id == "tt_patch"


def test_registration_refuses_when_the_handle_is_not_connected(monkeypatch):
    """A sole integration is not evidence it is the right account."""
    module = register_script()
    with_integrations(monkeypatch, module, [
        {"platform": "tiktok", "accountId": "tt_other", "username": "deployedunsafe"},
    ])

    with pytest.raises(SystemExit, match="0 tiktok integrations for @patchjohn"):
        module.verify_postpeer(Secrets(), "K", "tiktok", "@patchjohn")
