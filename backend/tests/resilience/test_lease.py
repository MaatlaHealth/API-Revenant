"""The instance lease (requirements 7.19, 7.26 to 7.31, 7.36 to 7.39).

Runs against the real lease row in a migrated database. In-memory deadlines use
an injected clock; staleness uses the database clock, so a stale lease is made
by moving ``heartbeat_at`` back in the row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError

from revenant.db.models import InstanceLease
from revenant.db.session import Database
from revenant.runtime.lease import Lease, LeaseUnavailable, SiblingWorker, Timing

TIMING = Timing(renew=5, grace=45, lifetime=30, margin=15, ttl=90, wait=120, trace_drain=5)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def lease(database: Database, clock: Clock, host: str = "host-a", **kwargs: Any) -> Lease:
    return Lease(database, TIMING, clock=clock, host=host, parent_pid=1, **kwargs)


def row(database: Database) -> InstanceLease:
    with database.session() as session:
        found = session.get(InstanceLease, 1)
        assert found is not None
        session.expunge(found)
        return found


def age_heartbeat(database: Database, seconds: float) -> None:
    """Make the lease look as if it was last renewed ``seconds`` ago, by the database clock."""
    with database.session() as session:
        now = session.scalar(text("SELECT CURRENT_TIMESTAMP"))
        moment = datetime.fromisoformat(str(now).replace(" ", "T")).replace(tzinfo=None)
        session.execute(update(InstanceLease).values(
            heartbeat_at=moment - timedelta(seconds=seconds)))


def break_database(monkeypatch: pytest.MonkeyPatch, database: Database) -> None:
    def unavailable() -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(database, "session", unavailable)


# ============================================================================= acquisition


def test_the_migration_creates_one_released_row(database: Database) -> None:
    found = row(database)
    assert (found.id, found.epoch, found.released, found.holder_id) == (1, 0, True, None)
    with pytest.raises(Exception), database.session() as session:  # noqa: B017, PT011
        session.execute(text("INSERT INTO instance_lease (id, epoch, beat, released) "
                             "VALUES (2, 0, 0, true)"))


def test_only_one_process_acquires(database: Database) -> None:
    clock = Clock()
    first, second = lease(database, clock), lease(database, clock, "host-b")
    assert first.state == "standby" and first.admit() == "standby"
    assert first.try_acquire() is True
    assert (first.state, first.epoch, first.admit()) == ("held", 1, None)
    assert second.try_acquire() is False and second.state == "standby"
    stored = row(database)
    assert (stored.holder_id, stored.epoch, stored.released) == (first.holder_id, 1, False)
    assert stored.heartbeat_at is not None


def test_two_processes_that_read_the_same_row_cannot_both_take_it(database: Database) -> None:
    """The compare-and-set: both saw a released lease; exactly one statement changes a row."""
    clock = Clock()
    first, second = lease(database, clock), lease(database, clock, "host-b")
    seen_by_first, _ = first._read()
    seen_by_second, _ = second._read()
    results = [first._take(seen_by_first), second._take(seen_by_second)]
    assert results == [True, False]
    assert row(database).epoch == 1 and second.state == "standby"


def test_a_renewal_between_read_and_take_defeats_the_take(database: Database) -> None:
    clock = Clock()
    holder, other = lease(database, clock), lease(database, clock, "host-b")
    holder.try_acquire()
    age_heartbeat(database, 200)          # looks dead to the other process
    seen, now = other._read()
    assert other._stale(seen, now)
    assert holder.renew() is True         # ...but it renews before the other acts
    assert other._take(seen) is False
    assert row(database).holder_id == holder.holder_id


def test_a_live_lease_is_never_taken_and_a_stale_one_is(database: Database) -> None:
    clock = Clock()
    holder, successor = lease(database, clock), lease(database, clock, "host-b")
    holder.try_acquire()
    for age in (0, 60, 89):
        age_heartbeat(database, age)
        assert successor.try_acquire() is False, age
    # The holder stopped renewing without releasing: after the TTL another process
    # acquires, with no manual step.
    age_heartbeat(database, 92)
    assert successor.try_acquire() is True
    assert (successor.epoch, row(database).holder_id) == (2, successor.holder_id)
    # The old holder finds out at its next renewal and serves nothing.
    lost: list[bool] = []
    holder.on_lost = lambda: lost.append(True)
    assert holder.renew() is False
    assert holder.state == "superseded" and lost == [True]
    assert holder.admit() == "superseded" and holder.may_send() is False
    assert holder.tick() is False         # it never asks for the lease back
    assert row(database).holder_id == successor.holder_id


def test_a_released_lease_is_acquired_at_once(database: Database) -> None:
    clock = Clock()
    holder, successor = lease(database, clock), lease(database, clock, "host-b")
    holder.try_acquire()
    holder.release()
    assert holder.state == "released" and holder.admit() == "superseded"
    assert row(database).released is True
    assert successor.try_acquire() is True and successor.epoch == 2
    holder.release()                       # releasing twice changes nothing
    assert row(database).holder_id == successor.holder_id and row(database).released is False


# ============================================================================= deadlines


def test_deadlines_run_from_the_last_confirmed_renewal(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    holder = lease(database, clock)
    holder.try_acquire()
    assert (holder.admit_until, holder.stop_by) == (1045.0, 1075.0)
    clock.now = 1020.0
    assert holder.renew() is True
    assert (holder.admit_until, holder.stop_by) == (1065.0, 1095.0)

    # The database goes away. Renewals fail; that is not proof of loss.
    break_database(monkeypatch, database)
    for moment in (1025.0, 1040.0, 1064.9):
        clock.now = moment
        assert holder.renew() is True and holder.database_ok is False
        assert holder.admit() is None, moment          # still serving, within grace
        assert (holder.admit_until, holder.stop_by) == (1065.0, 1095.0)   # never extended
    # Grace has run out: no new request is admitted. Requests already in progress
    # may still send until stop_by, and nothing after it.
    clock.now = 1065.0
    assert holder.admit() == "lease_expired" and holder.may_send() is True
    assert holder.describe()["state"] == "fenced"
    clock.now = 1094.9
    assert holder.may_send() is True
    clock.now = 1095.0
    assert holder.may_send() is False


def test_serving_resumes_when_the_database_returns_and_the_lease_is_still_ours(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    holder = lease(database, clock)
    holder.try_acquire()
    with monkeypatch.context() as patch:
        break_database(patch, database)
        clock.now = 1050.0
        holder.renew()
        assert holder.admit() == "lease_expired"
    clock.now = 1060.0
    assert holder.renew() is True and holder.database_ok is True
    assert holder.admit() is None and holder.admit_until == 1105.0
    assert holder.epoch == 1               # the same lease; nothing was re-acquired


def test_the_checks_on_the_request_path_use_no_database(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = lease(database, Clock())
    holder.try_acquire()
    break_database(monkeypatch, database)
    assert holder.admit() is None and holder.may_send() is True
    assert holder.describe()["valid_for_seconds"] == 45


def test_a_suspected_pause_stops_admission_until_a_renewal_confirms_ownership(
    database: Database
) -> None:
    clock = Clock()
    holder = lease(database, clock)
    holder.try_acquire()
    assert holder.tick() is True
    clock.now += 5
    assert holder.tick() is True and holder.suspect is False
    clock.now += 11                        # the thread was not scheduled for over 2 x renew
    holder._last_tick = clock.now - 11
    holder.suspect = False
    # The tick notices the gap first, then renews; the renewal clears the suspicion.
    seen: list[str | None] = []
    original = holder.renew

    def renew() -> bool:
        seen.append(holder.admit())
        return original()

    holder.renew = renew  # type: ignore[method-assign]
    assert holder.tick() is True
    assert seen == ["lease_expired"]       # nothing was admitted while it was unconfirmed
    assert holder.suspect is False and holder.admit() is None


# ============================================================================= handover


def test_a_sibling_worker_is_refused(database: Database) -> None:
    clock = Clock()
    lease(database, clock).try_acquire()
    sibling = lease(database, clock)       # same host, same parent process
    with pytest.raises(SiblingWorker) as excinfo:
        sibling.acquire_or_wait(sleep=lambda _seconds: None)
    assert excinfo.value.code == "unsupported_multi_worker"
    assert row(database).handover_requested_by is None


def test_handover_drains_before_release_and_never_overlaps(database: Database) -> None:
    clock = Clock()
    events: list[str] = []
    holder = lease(database, clock, on_handover=lambda: events.append("drain started"))
    holder.try_acquire()
    successor = lease(database, clock, "host-b")
    steps = iter(range(100))

    def sleep(_seconds: float) -> None:
        step = next(steps)
        clock.now += 1
        if step == 0:
            # The successor has asked. It has not taken anything.
            assert row(database).handover_requested_by == successor.holder_id
            assert successor.state == "standby" and holder.admit() is None
            assert holder.renew() is True
            # The holder stops accepting but keeps the lease while it drains.
            assert holder.state == "handover" and holder.admit() == "handover"
            assert holder.may_send() is True          # requests in progress may finish
            assert events == ["drain started"]
        elif step in (1, 2):
            assert holder.renew() is True             # renewal continues during the drain
            assert successor.state == "standby"
            assert row(database).holder_id == holder.holder_id
        elif step == 3:
            holder.release()                          # only after serving has stopped

    successor.acquire_or_wait(sleep=sleep)
    assert (successor.state, successor.epoch) == ("held", 2)
    stored = row(database)
    assert stored.holder_id == successor.holder_id and stored.handover_requested_by is None
    assert holder.admit() == "superseded" and holder.tick() is False


def test_a_waiting_process_gives_up_without_taking_a_live_lease(database: Database) -> None:
    clock = Clock()
    holder = lease(database, clock)
    holder.try_acquire()
    successor = lease(database, clock, "host-b")

    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        assert len(waits) < 100, "the waiting process never gave up"
        clock.now += 10
        holder.state = "held"              # a holder that never acts on the request
        holder.renew()

    with pytest.raises(LeaseUnavailable) as excinfo:
        successor.acquire_or_wait(sleep=sleep)
    assert excinfo.value.code == "lease_unavailable"
    assert len(waits) == 12                # 120 seconds of waiting, ten at a time
    assert row(database).holder_id == holder.holder_id and successor.state == "standby"


def test_a_waiting_process_does_not_ask_a_newer_holder(database: Database) -> None:
    clock = Clock()
    holder = lease(database, clock)
    holder.try_acquire()
    waiting, newer = lease(database, clock, "host-b"), lease(database, clock, "host-c")
    calls = iter(range(10))

    def sleep(_seconds: float) -> None:
        if next(calls) == 0:
            holder.release()
            assert newer.try_acquire() is True       # another new process got there first

    with pytest.raises(LeaseUnavailable, match="another new process"):
        waiting.acquire_or_wait(sleep=sleep)
    stored = row(database)
    # No second handover request: instances cannot take the lease from each other in turn.
    assert stored.holder_id == newer.holder_id and stored.handover_requested_by is None


def test_the_renewal_thread_enforces_the_handover_deadline(database: Database) -> None:
    """The event loop is hung and never drains. The renewal thread fences the
    process and releases the lease by itself."""
    clock = Clock()
    holder = lease(database, clock)
    holder.try_acquire()
    successor = lease(database, clock, "host-b")
    with database.session() as session:
        session.execute(update(InstanceLease).values(handover_requested_by=successor.holder_id))
    assert holder.tick() is True and holder.state == "handover"
    clock.now += TIMING.handover_deadline - 1
    holder._last_tick = clock.now
    assert holder.tick() is True and row(database).released is False
    clock.now += 2
    holder._last_tick = clock.now
    assert holder.tick() is False
    assert holder.state == "fenced" and holder.may_send() is False
    assert holder.admit() == "superseded" and row(database).released is True
    assert successor.try_acquire() is True


def test_force_acquire_takes_a_live_lease_only_when_asked(database: Database) -> None:
    clock = Clock()
    holder, developer = lease(database, clock), lease(database, clock, "host-b")
    holder.try_acquire()
    assert developer.try_acquire() is False
    assert developer.try_acquire(force=True) is True
    assert holder.renew() is False and holder.state == "superseded"


# ============================================================================= timing


def test_timing_must_satisfy_the_inequality() -> None:
    assert TIMING.problems() == []
    assert TIMING.ttl == TIMING.grace + TIMING.lifetime + TIMING.margin
    assert Timing(ttl=89.9).problems() == [
        "REVENANT_LEASE_TTL_SECONDS must be at least the grace period plus the request "
        "lifetime plus the clock margin"]
    assert "shorter than the grace period" in Timing(renew=45).problems()[0]
    assert "must be positive" in Timing(lifetime=0).problems()[0]
    assert TIMING.handover_deadline == 40


def test_heartbeats_use_the_database_clock(database: Database) -> None:
    holder = lease(database, Clock())      # a process clock that is wildly wrong
    holder.try_acquire()
    stored = row(database).heartbeat_at
    assert stored is not None
    written = stored if stored.tzinfo else stored.replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - written).total_seconds()) < 30
