"""Routing and credential selection. §8.2.1, §8.4.

The credential is a pair. A key belongs to the account whose id travels with
it, so handing one account another's token either fails or -- worse -- posts
somewhere unintended. These tests exist because that failure is silent.
"""

from __future__ import annotations

import json

import pytest

from socialq.models import Account
from socialq.registry import Registry, UnknownPublisher
from socialq.secrets import FileSecretStore


def credential(conn, label, ig_user_id, ref=None):
    conn.execute("INSERT INTO projects (id) VALUES ('deploysafe')"
                 " ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO publisher_credentials (project_id, publisher, label,"
        " api_key_ref, account_ids) VALUES ('deploysafe','instagram_graph',"
        " %s, %s, %s)",
        (label, ref or f"TOKEN_{label}", json.dumps({"instagram": ig_user_id})),
    )
    conn.commit()


def account(name, ig_user_id=None):
    return Account(id=1, project_id="deploysafe", name=name, platform="instagram",
                   handle=f"@{name}", publisher="instagram_graph",
                   external_id=ig_user_id)


@pytest.fixture
def registry(conn, tmp_path):
    store = FileSecretStore(tmp_path / "s.json")
    for label in ("first", "second", "deployedunsafe"):
        store.set(f"TOKEN_{label}", f"token-for-{label}")
    return Registry(conn, store)


def test_one_credential_one_account_resolves_by_name(conn, registry):
    credential(conn, "deployedunsafe", "1784100")
    built = registry(account("deployedunsafe", "1784100"))
    assert built.access_token == "token-for-deployedunsafe"


def test_a_single_credential_is_NOT_assumed_to_fit_any_account(conn, registry):
    """Regression, 2026-09-22. This published a 1x1 image to a real account:
    an unrelated account with no credential of its own was handed the only
    credential in the database, because "there is only one" was mistaken for
    "it must be the right one". An unpaired account is a configuration error
    and the safe response is to refuse."""
    credential(conn, "deployedunsafe", "1784100")

    with pytest.raises(UnknownPublisher, match="no credential paired"):
        registry(account("unrelated", "9999999"))


def test_an_unpaired_account_is_refused_even_with_no_platform_id(conn, registry):
    credential(conn, "deployedunsafe", "1784100")

    with pytest.raises(UnknownPublisher, match="no credential paired"):
        registry(account("unrelated"))


def test_each_account_gets_its_own_token(conn, registry):
    """The bug this file exists for: a second account must not be handed the
    first account's token."""
    credential(conn, "first", "1784111")
    credential(conn, "second", "1784222")

    assert registry(account("first", "1784111")).access_token == "token-for-first"
    assert registry(account("second", "1784222")).access_token == "token-for-second"


def test_the_platform_id_wins_over_row_order(conn, registry):
    """Matching on the id, not on whichever row was inserted first."""
    credential(conn, "first", "1784111")
    credential(conn, "second", "1784222")

    built = registry(account("renamed-since", "1784222"))
    assert built.access_token == "token-for-second"
    assert built.ig_user_id == "1784222"


def test_the_label_matches_when_no_platform_id_is_known(conn, registry):
    """An account registered before its external_id was recorded."""
    credential(conn, "first", "1784111")
    credential(conn, "second", "1784222")

    assert registry(account("second")).access_token == "token-for-second"


def test_an_unmatched_account_is_refused_rather_than_guessed(conn, registry):
    """Guessing here means posting to the wrong account."""
    credential(conn, "first", "1784111")
    credential(conn, "second", "1784222")

    with pytest.raises(UnknownPublisher, match="no credential paired"):
        registry(account("third", "1784333"))


def test_a_disabled_credential_is_not_selected(conn, registry):
    credential(conn, "first", "1784111")
    credential(conn, "second", "1784222")
    conn.execute("UPDATE publisher_credentials SET enabled = false"
                 " WHERE label = 'second'")
    conn.commit()

    assert registry(account("first", "1784111")).access_token == "token-for-first"
    # The disabled credential's account must not fall through to the enabled
    # one just because it is the only row left.
    with pytest.raises(UnknownPublisher):
        registry(account("second", "1784222"))


def test_no_credential_at_all_is_a_clear_error(conn, registry):
    with pytest.raises(UnknownPublisher, match="no instagram_graph credential"):
        registry(account("nobody", "1784111"))
