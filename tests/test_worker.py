"""The worker state machine. §6.

The important property is the ordering in §6: the attempt row is committed
before the network call, so a crash leaves evidence that a call may have
happened. Several of these tests exist only to pin that down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from socialq import repo
from socialq.publishers.base import (
    AuthError,
    OutOfCreditError,
    PublishError,
    RateLimitedError,
    Retryable,
)
from socialq.worker import MAX_ATTEMPTS, Worker, backoff, next_month

from helpers import NOW, FakePublisher, attempt_rows, seed, target_row


def worker(conn, publisher, **kw):
    return Worker(conn, lambda account: publisher, worker_id="w1", now=lambda: NOW, **kw)


# -- backoff -------------------------------------------------------------


def test_backoff_grows_exponentially_and_is_capped_at_six_hours():
    assert timedelta(minutes=2) <= backoff(1) < timedelta(minutes=3)
    assert timedelta(minutes=16) <= backoff(4) < timedelta(minutes=20)
    assert backoff(20) <= timedelta(hours=6) * 1.2


def test_backoff_is_jittered_so_failures_do_not_resynchronise():
    assert len({backoff(3) for _ in range(20)}) > 1


def test_next_month_rolls_the_year():
    assert next_month(datetime(2026, 12, 9, tzinfo=timezone.utc)) == datetime(
        2027, 1, 1, tzinfo=timezone.utc
    )


# -- claiming ------------------------------------------------------------


def test_two_workers_racing_one_row_produce_exactly_one_winner(conn, database_url):
    """§13. The claim is the one query where correctness cannot be argued."""
    target = seed(conn)

    a = psycopg.connect(database_url, row_factory=dict_row)
    b = psycopg.connect(database_url, row_factory=dict_row)
    try:
        # A claims inside an open transaction, holding the row lock.
        claimed_a = repo.claim(a, "worker-a", 10)
        # B must skip the locked row rather than block or double-claim.
        claimed_b = repo.claim(b, "worker-b", 10)
        a.commit()
        b.commit()
    finally:
        a.close()
        b.close()

    ids = [t.id for t in claimed_a] + [t.id for t in claimed_b]
    assert ids == [target]
    assert target_row(conn, target)["claimed_by"] in ("worker-a", "worker-b")


def test_a_target_scheduled_for_later_is_not_claimed(conn):
    # Relative to the database clock, not the tests' fixed NOW: the claim
    # compares against now(), so a fixed date silently expires into the past.
    seed(conn, scheduled_for=datetime.now(timezone.utc) + timedelta(days=1))
    assert repo.claim(conn, "w1", 10) == []


def test_a_disabled_account_is_not_claimed(conn):
    # §14: accounts.enabled exists so a new account can be warmed by hand.
    seed(conn, enabled=False)
    assert repo.claim(conn, "w1", 10) == []


def test_a_target_in_backoff_is_not_claimed_until_its_time(conn):
    seed(conn, next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert repo.claim(conn, "w1", 10) == []


# -- the happy path ------------------------------------------------------


def test_a_published_target_records_the_provider_id_and_permalink(conn):
    target = seed(conn)
    stats = worker(conn, FakePublisher()).run_once()

    row = target_row(conn, target)
    assert (stats.published, row["state"]) == (1, "published")
    assert row["provider_post_id"] == "P1"
    assert row["permalink"] == "https://x/1"
    assert row["last_error"] is None


def test_the_attempt_row_is_written_before_the_publisher_is_called(conn, database_url):
    """§6 step 2. Without this, a crash between the call and the record is
    indistinguishable from a call that never happened."""
    target = seed(conn)
    seen = {}

    class Observer(FakePublisher):
        def publish(self, t, post, media):
            # A separate connection sees only committed data.
            with psycopg.connect(database_url, row_factory=dict_row) as other:
                seen["attempts"] = attempt_rows(other, target)
                seen["state"] = target_row(other, target)["state"]
            return super().publish(t, post, media)

    worker(conn, Observer()).run_once()

    assert seen["state"] == "in_flight"
    assert len(seen["attempts"]) == 1
    assert seen["attempts"][0]["idem_key"] == f"target:{target}:1"


def test_the_idem_key_is_the_attempt_number(conn):
    target = seed(conn, attempts=2)
    worker(conn, FakePublisher()).run_once()
    assert attempt_rows(conn, target)[0]["idem_key"] == f"target:{target}:3"


# -- failure -------------------------------------------------------------


def test_a_transient_failure_goes_back_to_pending_with_backoff(conn):
    target = seed(conn)
    stats = worker(conn, FakePublisher(error=Retryable("timeout"))).run_once()

    row = target_row(conn, target)
    assert (stats.retried, row["state"], row["attempts"]) == (1, "pending", 1)
    assert row["next_attempt_at"] > NOW
    assert "timeout" in row["last_error"]


def test_the_failure_is_recorded_on_the_attempt_row(conn):
    target = seed(conn)
    worker(conn, FakePublisher(error=PublishError("nope", raw={"code": 9}))).run_once()

    response = attempt_rows(conn, target)[0]["response"]
    assert response["ok"] is False
    assert response["raw"] == {"code": 9}


def test_a_target_dies_after_the_attempt_cap(conn):
    target = seed(conn, attempts=MAX_ATTEMPTS - 1)
    stats = worker(conn, FakePublisher(error=PublishError("nope"))).run_once()

    row = target_row(conn, target)
    assert (stats.dead, row["state"], row["attempts"]) == (1, "dead", MAX_ATTEMPTS)
    assert row["next_attempt_at"] is None


def test_a_dead_target_is_never_claimed_again(conn):
    seed(conn, attempts=MAX_ATTEMPTS - 1)
    worker(conn, FakePublisher(error=PublishError("nope"))).run_once()
    assert repo.claim(conn, "w1", 10) == []


# -- waiting is not failing ----------------------------------------------


def test_out_of_credit_waits_for_next_month_and_spends_no_attempt(conn):
    """§8.2.1(3): running out of credit is a wait, not an error."""
    target = seed(conn)
    stats = worker(conn, FakePublisher(error=OutOfCreditError("no credit"))).run_once()

    row = target_row(conn, target)
    assert (stats.waiting, row["state"], row["attempts"]) == (1, "pending", 0)
    assert row["next_attempt_at"] == next_month(NOW)


def test_out_of_credit_never_kills_a_target_however_often_it_happens(conn):
    target = seed(conn, attempts=MAX_ATTEMPTS - 1)
    worker(conn, FakePublisher(error=OutOfCreditError("no credit"))).run_once()
    assert target_row(conn, target)["state"] == "pending"


def test_a_rate_limit_waits_and_spends_no_attempt(conn):
    target = seed(conn)
    retry_at = NOW + timedelta(hours=3)
    stats = worker(
        conn, FakePublisher(error=RateLimitedError("slow down", retry_after=retry_at))
    ).run_once()

    row = target_row(conn, target)
    assert (stats.waiting, row["attempts"]) == (1, 0)
    assert row["next_attempt_at"] == retry_at


# -- auth ----------------------------------------------------------------


def test_an_auth_error_disables_the_account_and_alerts(conn):
    """§8.2.1: one dead credential must never stall the queue."""
    target = seed(conn)
    alerts = []

    w = worker(conn, FakePublisher(error=AuthError("token expired")))
    w.alert = alerts.append
    w.run_once()

    account_id = target_row(conn, target)["account_id"]
    enabled = conn.execute(
        "SELECT enabled FROM accounts WHERE id = %s", (account_id,)
    ).fetchone()["enabled"]
    assert enabled is False
    assert alerts and "token expired" in alerts[0]
    assert target_row(conn, target)["state"] == "pending"


# -- isolation -----------------------------------------------------------


def test_one_failing_target_does_not_take_the_batch_down(conn):
    good = seed(conn)
    bad = seed(conn)

    class Selective(FakePublisher):
        def publish(self, target, post, media):
            if target.id == bad:
                raise PublishError("this one only")
            return super().publish(target, post, media)

    stats = worker(conn, Selective()).run_once()

    assert (stats.claimed, stats.published, stats.retried) == (2, 1, 1)
    assert target_row(conn, good)["state"] == "published"
    assert target_row(conn, bad)["state"] == "pending"


def test_an_unhandled_error_still_leaves_the_target_retryable(conn):
    target = seed(conn)

    class Exploding(FakePublisher):
        def publish(self, target, post, media):
            raise ValueError("something nobody anticipated")

    stats = worker(conn, Exploding()).run_once()

    assert stats.errors
    assert target_row(conn, target)["state"] == "pending"
    assert target_row(conn, target)["attempts"] == 1


def test_a_claimed_target_abandoned_by_a_dead_worker_is_released(conn):
    """A crash before the attempt insert is safe to reopen: no call happened."""
    target = seed(conn)
    repo.claim(conn, "dead-worker", 10)
    conn.commit()

    released = repo.release_claimed(conn, datetime.now(timezone.utc) + timedelta(hours=1))
    conn.commit()

    assert released == 1
    assert target_row(conn, target)["state"] == "pending"
    assert [t.id for t in repo.claim(conn, "w2", 10)] == [target]
