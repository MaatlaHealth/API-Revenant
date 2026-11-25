"""The instance lease (docs/architecture.md, section 4.5).

One row in the application's own database says which process may serve runtime
traffic. Under the assumptions documented there, at most one process serves at
a time. This is not a clustering mechanism and it is not an unconditional
guarantee: there is no fencing token the replacement API could check.

All timestamps in the row are written and compared with the *database* clock.
In-memory deadlines use a clock that keeps counting during a suspend where the
operating system offers one.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from revenant.config import Settings
from revenant.db.models import InstanceLease
from revenant.db.session import Database

log = structlog.get_logger("revenant.lease")


class LeaseUnavailable(RuntimeError):
    """The lease could not be acquired; the process must not serve."""

    code = "lease_unavailable"


class SiblingWorker(LeaseUnavailable):
    """Another worker of the same server already holds the lease."""

    code = "unsupported_multi_worker"


def suspend_aware_clock() -> float:
    """Seconds on a clock that keeps counting while the machine is suspended, if any."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    read = getattr(time, "clock_gettime", None)
    if boottime is not None and read is not None:
        return float(read(boottime))
    return time.monotonic()


@dataclass(frozen=True)
class Timing:
    renew: float = 5.0
    grace: float = 45.0
    lifetime: float = 30.0
    margin: float = 15.0
    ttl: float = 90.0
    wait: float = 120.0
    trace_drain: float = 5.0

    @classmethod
    def from_settings(cls, settings: Settings) -> Timing:
        return cls(settings.lease_renew_seconds, settings.lease_grace_seconds,
                   settings.request_lifetime_seconds, settings.lease_clock_margin_seconds,
                   settings.lease_ttl_seconds, settings.lease_wait_seconds,
                   settings.trace_drain_seconds)

    def problems(self) -> list[str]:
        found = []
        if min(self.renew, self.grace, self.lifetime, self.ttl) <= 0 or self.margin < 0:
            found.append("lease timings must be positive")
        if self.ttl < self.grace + self.lifetime + self.margin:
            found.append("REVENANT_LEASE_TTL_SECONDS must be at least the grace period plus the "
                         "request lifetime plus the clock margin")
        if self.renew >= self.grace:
            found.append("REVENANT_LEASE_RENEW_SECONDS must be shorter than the grace period")
        return found

    @property
    def handover_deadline(self) -> float:
        return self.lifetime + self.trace_drain + 5.0


def _utc(value: Any) -> datetime | None:
    """Database timestamps as aware UTC datetimes, whatever the driver returns."""
    if value is None:
        return None
    moment: datetime = (datetime.fromisoformat(value.replace(" ", "T"))
                        if isinstance(value, str) else value)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _changed(result: Any) -> int:
    """Rows changed by an UPDATE."""
    return int(getattr(result, "rowcount", 0) or 0)


class Lease:
    """Acquire, renew, hand over and release the lease; answer in-memory checks.

    ``admit()`` and ``may_send()`` never touch the database: they compare the
    clock with deadlines set by the last *successful* renewal.
    """

    def __init__(self, database: Database, timing: Timing, *,
                 clock: Callable[[], float] = suspend_aware_clock,
                 host: str | None = None, parent_pid: int | None = None,
                 on_lost: Callable[[], None] | None = None,
                 on_handover: Callable[[], None] | None = None) -> None:
        self.database = database
        self.timing = timing
        self.clock = clock
        self.holder_id = uuid.uuid4().hex
        self.host = host if host is not None else socket.gethostname()
        self.parent_pid = parent_pid if parent_pid is not None else os.getppid()
        self.on_lost = on_lost
        self.on_handover = on_handover
        self.epoch = 0
        self.state = "standby"  # standby | held | handover | superseded | fenced | released
        self.accepting = False
        self.admit_until = 0.0
        self.stop_by = 0.0
        self.last_renewed_at: datetime | None = None
        self.database_ok = True
        self.suspect = False
        self._handover_seen: float | None = None
        self._last_tick: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ database

    def _read(self) -> tuple[InstanceLease, datetime]:
        with self.database.session() as session:
            row = session.get(InstanceLease, 1)
            now = _utc(session.scalar(select(func.current_timestamp())))
            if row is None or now is None:
                raise LeaseUnavailable("the lease row is missing; run the migrations")
            session.expunge(row)
            return row, now

    def _take(self, seen: InstanceLease) -> bool:
        """One atomic compare-and-set against exactly the row that was read.

        A renewal or another acquisition between the read and this statement
        changes ``beat`` or ``epoch``, so the statement changes no row.
        """
        holder = (InstanceLease.holder_id.is_(None) if seen.holder_id is None
                  else InstanceLease.holder_id == seen.holder_id)
        with self.database.session() as session:
            sent = self.clock()
            changed = _changed(session.execute(
                update(InstanceLease)
                .where(InstanceLease.id == 1, InstanceLease.epoch == seen.epoch, holder,
                       InstanceLease.beat == seen.beat, InstanceLease.released == seen.released)
                .values(holder_id=self.holder_id, host=self.host, parent_pid=self.parent_pid,
                        epoch=InstanceLease.epoch + 1, beat=InstanceLease.beat + 1,
                        heartbeat_at=func.current_timestamp(),
                        released=False, handover_requested_at=None, handover_requested_by=None)
            ))
        if changed != 1:
            return False
        with self._lock:
            self.epoch = seen.epoch + 1
            self.state, self.accepting, self.suspect = "held", True, False
            self._extend(sent)
        log.info("lease_acquired", epoch=self.epoch)
        return True

    def _extend(self, sent: float) -> None:
        # Deadlines run from the moment the renewal was *sent*, not acknowledged.
        self.admit_until = sent + self.timing.grace
        self.stop_by = self.admit_until + self.timing.lifetime
        self.last_renewed_at = datetime.now(UTC)
        self.database_ok = True

    def _stale(self, row: InstanceLease, now: datetime) -> bool:
        heartbeat = _utc(row.heartbeat_at)
        return row.released or heartbeat is None or (
            now - heartbeat).total_seconds() > self.timing.ttl

    def try_acquire(self, *, force: bool = False) -> bool:
        row, now = self._read()
        if force or self._stale(row, now):
            return self._take(row)
        return False

    def acquire_or_wait(self, *, force: bool = False,
                        sleep: Callable[[float], None] = time.sleep) -> None:
        """Acquire at start-up, asking a live holder to hand over. Never takes a
        live lease. Raises ``LeaseUnavailable`` when the wait is exhausted."""
        deadline = self.clock() + self.timing.wait
        asked: tuple[str | None, int] | None = None
        while True:
            row, now = self._read()
            if force or self._stale(row, now):
                if self._take(row):
                    return
            elif asked is None:
                if (row.host, row.parent_pid) == (self.host, self.parent_pid):
                    raise SiblingWorker(
                        "another worker of this server holds the lease; exactly one worker is "
                        "supported")
                asked = (row.holder_id, row.epoch)
                with self.database.session() as session:
                    session.execute(
                        update(InstanceLease)
                        .where(InstanceLease.id == 1, InstanceLease.holder_id == row.holder_id,
                               InstanceLease.epoch == row.epoch,
                               InstanceLease.released.is_(False))
                        .values(handover_requested_at=func.current_timestamp(),
                                handover_requested_by=self.holder_id))
                log.info("lease_handover_requested", holder_epoch=row.epoch)
            elif (row.holder_id, row.epoch) != asked:
                # Someone newer than the holder we asked took it. Do not ask again.
                raise LeaseUnavailable("the lease was acquired by another new process")
            if self.clock() >= deadline:
                raise LeaseUnavailable("the lease could not be acquired within the configured wait")
            sleep(min(1.0, self.timing.renew))

    def renew(self) -> bool:
        """One renewal. Returns False when ownership has been lost."""
        sent = self.clock()
        try:
            with self.database.session() as session:
                changed = _changed(session.execute(
                    update(InstanceLease)
                    .where(InstanceLease.id == 1, InstanceLease.holder_id == self.holder_id,
                           InstanceLease.epoch == self.epoch, InstanceLease.released.is_(False))
                    .values(heartbeat_at=func.current_timestamp(),
                            beat=InstanceLease.beat + 1)))
                requested = session.scalar(
                    select(InstanceLease.handover_requested_by).where(InstanceLease.id == 1))
        except SQLAlchemyError:
            # Not proof of loss: keep serving until the grace period runs out.
            self.database_ok = False
            return True
        if changed != 1:
            with self._lock:
                self.state, self.accepting = "superseded", False
            log.error("lease_lost", epoch=self.epoch)
            if self.on_lost:
                self.on_lost()
            return False
        begin_handover = False
        with self._lock:
            self._extend(sent)
            self.suspect = False
            if requested and self.state == "held":
                self.state, self.accepting = "handover", False
                self._handover_seen = sent
                begin_handover = True
        if begin_handover:
            log.info("lease_handover_started", epoch=self.epoch)
            if self.on_handover:
                self.on_handover()
        return True

    def release(self) -> None:
        """Give the lease up. Call only after serving has stopped."""
        with self._lock:
            if self.state in ("superseded", "released", "standby"):
                return
            self.accepting = False
            self.state = "released"
        try:
            with self.database.session() as session:
                session.execute(
                    update(InstanceLease)
                    .where(InstanceLease.id == 1, InstanceLease.holder_id == self.holder_id,
                           InstanceLease.epoch == self.epoch)
                    .values(released=True))
            log.info("lease_released", epoch=self.epoch)
        except SQLAlchemyError:
            log.error("lease_release_failed", epoch=self.epoch)

    # ------------------------------------------------------------------ renewal thread

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lease-renewal", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timing.renew + 5)

    def tick(self) -> bool:
        """One pass of the renewal thread. Returns False when the thread should end."""
        now = self.clock()
        if self._last_tick is not None and now - self._last_tick > 2 * self.timing.renew:
            # The thread was not scheduled for a long time: the process may have
            # been paused. Admit nothing until a renewal confirms ownership.
            with self._lock:
                self.suspect = True
            log.warning("lease_pause_suspected")
        self._last_tick = now
        if self.state in ("superseded", "released", "fenced", "standby"):
            return False
        if (self.state == "handover" and self._handover_seen is not None
                and now - self._handover_seen > self.timing.handover_deadline):
            # The event loop did not finish draining. Fence from this thread,
            # independently of it, and let the successor in.
            with self._lock:
                self.accepting = False
            log.error("lease_handover_deadline_exceeded")
            self.release()
            with self._lock:
                self.state = "fenced"
            return False
        return self.renew()

    def _run(self) -> None:
        while not self._stop.wait(self.timing.renew):
            if not self.tick():
                return

    # ------------------------------------------------------------------ in-memory checks

    def admit(self) -> str | None:
        """Why a new runtime request may not be admitted, or None. No database access."""
        if self.state == "handover":
            return "handover"
        if self.state in ("superseded", "released", "fenced"):
            return "superseded"
        if self.state != "held":
            return "standby"
        if not self.accepting:
            return "handover"
        if self.suspect or self.clock() >= self.admit_until:
            return "lease_expired"
        return None

    def may_send(self) -> bool:
        """The second check, immediately before an upstream call. No database access."""
        return (self.state in ("held", "handover") and not self.suspect
                and self.clock() < self.stop_by)

    def stop_accepting(self) -> None:
        with self._lock:
            self.accepting = False

    def describe(self) -> dict[str, Any]:
        remaining = max(0.0, self.admit_until - self.clock()) if self.state == "held" else 0.0
        state = self.state
        if state == "held" and (self.suspect or remaining <= 0):
            state = "fenced"
        return {"state": {"released": "standby", "superseded": "standby"}.get(state, state),
                "detail": self.state, "epoch": self.epoch,
                "last_renewed_at": self.last_renewed_at,
                "valid_for_seconds": int(remaining)}
