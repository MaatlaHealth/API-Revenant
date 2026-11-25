"""The trace pipeline (docs/architecture.md, section 4.9).

Tracing is best-effort observability. It never delays or fails a proxy request,
never grows without bound, and never claims a trace was stored when it was not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import Counter
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy import delete, func, select

from revenant.db.models import RuntimeEvent
from revenant.db.session import Database

log = structlog.get_logger("revenant.traces")

MAX_TRACE_BYTES = 32 * 1024
BATCH_SIZE = 100
BATCH_WAIT = 0.5
RETRY_DELAYS = (0.5, 1.0, 2.0)
_COLUMNS = ("project_id", "request_id", "kind", "entry", "original_host", "method", "legacy_path",
            "legacy_query", "query_credential_removed", "upstream_path", "route_id",
            "legacy_status", "upstream_status", "duration_ms", "upstream_ms", "outcome", "trace",
            "adapter_version_id", "created_at")


def cap(event: dict[str, Any]) -> dict[str, Any]:
    """Keep one event under the size cap by dropping its snapshots, largest first."""
    trace = event.get("trace") or {}
    if len(json.dumps(trace, default=str)) <= MAX_TRACE_BYTES:
        return event
    trace = {**trace, "truncated": True}
    snapshots = ("upstream_response", "legacy_response", "upstream_request", "legacy_request",
                 "rules")
    by_size = sorted((key for key in snapshots if key in trace),
                     key=lambda key: len(json.dumps(trace[key], default=str)), reverse=True)
    for key in by_size:
        trace[key] = {"omitted": "larger than the trace size limit"}
        if len(json.dumps(trace, default=str)) <= MAX_TRACE_BYTES:
            return {**event, "trace": trace}
    return {**event, "trace": {"truncated": True}}


class TracePipeline:
    def __init__(self, database: Database, queue_size: int, retention: int = 5000, *,
                 persist: Callable[[list[dict[str, Any]]], None] | None = None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.database = database
        self.queue_size = queue_size
        self.retention = retention
        self.retry_delays = retry_delays
        self._persist = persist or self._store
        self.queue: asyncio.Queue[dict[str, Any]] | None = None
        self.generated = 0
        self.accepted = 0
        self.persisted = 0
        self.dropped = {"queue_full": 0, "persist_failed": 0, "shutdown": 0}
        self.in_flight = 0
        self.writer = "stopped"  # running | retrying | stopped
        self.last_persist_error: str | None = None
        # Losses not yet recorded as a trace_loss event: (project, reason) -> count.
        self._unrecorded: Counter[tuple[str, str]] = Counter()
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------------ request path

    def offer(self, event: dict[str, Any]) -> str:
        """Hand over one event without waiting. Returns ``queued`` or ``dropped``."""
        self.generated += 1
        if self.queue is None or self._closing:
            # Never accepted, so it belongs with the events refused at the door:
            # generated = accepted + dropped["queue_full"] always holds.
            self._lose(event, "queue_full")
            return "dropped"
        try:
            self.queue.put_nowait(cap(event))
        except asyncio.QueueFull:
            self._lose(event, "queue_full")
            return "dropped"
        self.accepted += 1
        return "queued"

    def _lose(self, event: dict[str, Any], reason: str, count: int = 1) -> None:
        self.dropped[reason] += count
        self._unrecorded[(str(event.get("project_id")), reason)] += count

    # ------------------------------------------------------------------ writer

    def start(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.queue_size)
        self._closing = False
        self._task = asyncio.get_running_loop().create_task(self._supervise())

    async def _supervise(self) -> None:
        """Restart the writer if it ever exits unexpectedly."""
        delay = 0.5
        while not self._closing:
            self.writer = "running"
            try:
                await self._write()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the writer must not die silently
                self.writer = "stopped"
                log.error("trace_writer_crashed", error_type=type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    async def _next_batch(self, queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
        batch = [await queue.get()]
        # Counted as in flight from the moment it leaves the queue, so an event is
        # always in exactly one place: queued, in flight, persisted or dropped.
        self.in_flight = 1
        deadline = asyncio.get_running_loop().time() + BATCH_WAIT
        while len(batch) < BATCH_SIZE:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), remaining))
                self.in_flight = len(batch)
            except TimeoutError:
                break
        return batch

    async def _write(self) -> None:
        queue = self.queue
        while queue is not None:
            batch = await self._next_batch(queue)
            await self._flush(batch)

    async def _flush(self, batch: list[dict[str, Any]], *, retry: bool = True) -> None:
        self.in_flight = len(batch)
        try:
            delays = self.retry_delays if retry else ()
            for attempt in range(len(delays) + 1):
                try:
                    recorded = Counter(self._unrecorded)
                    losses = self._pending_losses()
                    await asyncio.to_thread(self._persist, batch + losses)
                except Exception as exc:  # noqa: BLE001 - any storage failure is handled the same
                    self.last_persist_error = type(exc).__name__
                    if attempt == len(delays):
                        for event in batch:
                            self._lose(event, "persist_failed")
                        log.error("traces_dropped", count=len(batch), reason="persist_failed",
                                  error_type=type(exc).__name__)
                        return
                    self.writer = "retrying"
                    await asyncio.sleep(delays[attempt])
                else:
                    self.persisted += len(batch)
                    # Only what was written is forgotten; losses that happened
                    # while the batch was being stored are recorded next time.
                    self._unrecorded -= recorded
                    self.last_persist_error = None
                    self.writer = "running"
                    return
        finally:
            self.in_flight = 0

    def _pending_losses(self) -> list[dict[str, Any]]:
        """One ``trace_loss`` event per project for losses not yet recorded, so a
        loss is visible in the event history and not only in live counters."""
        by_project: dict[str, dict[str, int]] = {}
        for (project_id, reason), count in self._unrecorded.items():
            if project_id and project_id != "None":
                by_project.setdefault(project_id, {})[reason] = count
        return [{"project_id": project_id, "kind": "trace_loss", "outcome": "trace_loss",
                 "trace": {"dropped": reasons, "total": sum(reasons.values())}}
                for project_id, reasons in sorted(by_project.items())]

    def _store(self, events: list[dict[str, Any]]) -> None:
        with self.database.session() as session:
            for event in events:
                session.add(RuntimeEvent(**{key: event[key] for key in _COLUMNS if key in event}))
            session.flush()
            for project_id in {event["project_id"] for event in events}:
                self._prune(session, project_id)

    def _prune(self, session: Any, project_id: str) -> None:
        total = session.scalar(select(func.count()).select_from(RuntimeEvent).where(
            RuntimeEvent.project_id == project_id)) or 0
        if total <= self.retention:
            return
        keep = select(RuntimeEvent.id).where(RuntimeEvent.project_id == project_id).order_by(
            RuntimeEvent.created_at.desc(), RuntimeEvent.id).limit(self.retention)
        session.execute(delete(RuntimeEvent).where(
            RuntimeEvent.project_id == project_id, RuntimeEvent.id.not_in(keep)))

    # ------------------------------------------------------------------ shutdown

    async def drain(self, seconds: float) -> None:
        """Persist what is queued, within the deadline. The rest is counted as dropped."""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self.queue is None:
            return
        current: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(seconds):
                while not self.queue.empty():
                    current = [self.queue.get_nowait()
                               for _ in range(min(BATCH_SIZE, self.queue.qsize()))]
                    await self._flush(current, retry=False)
                    current = []
        except TimeoutError:
            # The batch being written when time ran out is not known to be stored,
            # so it is counted as lost rather than assumed saved.
            for event in current:
                self._lose(event, "shutdown")
        left = 0
        while not self.queue.empty():
            self._lose(self.queue.get_nowait(), "shutdown")
            left += 1
        if left:
            log.error("traces_dropped", count=left, reason="shutdown")
        self.writer = "stopped"

    def describe(self) -> dict[str, Any]:
        depth = self.queue.qsize() if self.queue is not None else 0
        return {"writer": self.writer, "queue_depth": depth, "queue_capacity": self.queue_size,
                "in_flight": self.in_flight, "generated": self.generated,
                "accepted": self.accepted, "persisted": self.persisted,
                "dropped": dict(self.dropped), "last_persist_error": self.last_persist_error}
