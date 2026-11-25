"""The trace pipeline (requirements 7.7, 7.14 to 7.17): bounded, best-effort,
and never claiming that a trace was stored when it was not."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from sqlalchemy import select

from revenant.db.models import RuntimeEvent
from revenant.db.session import Database
from revenant.runtime.traces import MAX_TRACE_BYTES, TracePipeline, cap


def event(project_id: str, number: int = 0, **extra: Any) -> dict[str, Any]:
    return {"project_id": project_id, "request_id": f"req-{number}", "kind": "request",
            "outcome": "ok", "method": "GET", "legacy_path": f"/v1/charges/{number}",
            "trace": {"n": number}, **extra}


def identities(pipeline: TracePipeline) -> None:
    """The two identities that must hold after every scenario."""
    state = pipeline.describe()
    dropped = state["dropped"]
    assert state["generated"] == state["accepted"] + dropped["queue_full"]
    assert state["accepted"] == (state["persisted"] + dropped["persist_failed"]
                                 + dropped["shutdown"] + state["queue_depth"]
                                 + state["in_flight"])


def stored(database: Database, kind: str = "request") -> list[RuntimeEvent]:
    with database.session() as session:
        rows = list(session.scalars(select(RuntimeEvent).where(RuntimeEvent.kind == kind)
                                    .order_by(RuntimeEvent.request_id)))
        session.expunge_all()
        return rows


async def settle(pipeline: TracePipeline, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        state = pipeline.describe()
        if state["queue_depth"] == 0 and state["in_flight"] == 0:
            return
        await asyncio.sleep(0.02)


def test_events_are_persisted_in_batches(database: Database, project_id: str) -> None:
    async def scenario() -> TracePipeline:
        pipeline = TracePipeline(database, queue_size=500)
        pipeline.start()
        assert [pipeline.offer(event(project_id, n)) for n in range(250)] == ["queued"] * 250
        await settle(pipeline)
        identities(pipeline)
        await pipeline.drain(5)
        return pipeline

    pipeline = asyncio.run(scenario())
    state = pipeline.describe()
    assert (state["accepted"], state["persisted"]) == (250, 250)
    assert state["dropped"] == {"queue_full": 0, "persist_failed": 0, "shutdown": 0}
    assert len(stored(database)) == 250 and state["writer"] == "stopped"
    identities(pipeline)


def test_a_full_queue_drops_without_waiting_and_memory_stays_bounded(
    database: Database, project_id: str
) -> None:
    release = threading.Event()

    def slow(_events: list[dict[str, Any]]) -> None:
        release.wait(10)

    async def scenario() -> TracePipeline:
        pipeline = TracePipeline(database, queue_size=5, persist=slow)
        pipeline.start()
        pipeline.offer(event(project_id, 0))
        await asyncio.sleep(0.8)                 # the writer is now stuck storing one event
        started = asyncio.get_running_loop().time()
        fates = [pipeline.offer(event(project_id, n)) for n in range(1, 1001)]
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 5.0                     # never waited for the writer (stuck 10 s)
        assert fates.count("queued") == 5 and fates.count("dropped") == 995
        assert pipeline.queue is not None and pipeline.queue.qsize() == 5
        identities(pipeline)
        release.set()
        await settle(pipeline)
        await pipeline.drain(5)
        return pipeline

    pipeline = asyncio.run(scenario())
    assert pipeline.describe()["dropped"]["queue_full"] == 995
    assert pipeline.generated == 1001 and pipeline.accepted == 6
    identities(pipeline)


def test_persistence_failures_are_retried_then_counted_and_recorded(
    database: Database, project_id: str
) -> None:
    attempts: list[int] = []
    healthy = threading.Event()
    real = TracePipeline(database, queue_size=10)

    def flaky(events: list[dict[str, Any]]) -> None:
        if not healthy.is_set():
            attempts.append(len(events))
            raise ConnectionError("database is away password=hunter2")
        real._store(events)

    async def scenario() -> TracePipeline:
        pipeline = TracePipeline(database, queue_size=10, persist=flaky,
                                 retry_delays=(0.01, 0.01, 0.01))
        pipeline.start()
        for n in range(3):
            assert pipeline.offer(event(project_id, n)) == "queued"   # accepted, not "stored"
        await settle(pipeline)
        assert len(attempts) == 4                 # one try and three retries, then give up
        state = pipeline.describe()
        assert state["persisted"] == 0 and state["dropped"]["persist_failed"] == 3
        assert state["last_persist_error"] == "ConnectionError"
        assert "hunter2" not in json.dumps(state)
        identities(pipeline)
        # Storage comes back. The next batch succeeds and the loss is written down.
        healthy.set()
        pipeline.offer(event(project_id, 10))
        await settle(pipeline)
        assert pipeline.describe()["writer"] == "running"
        identities(pipeline)
        await pipeline.drain(5)
        return pipeline

    pipeline = asyncio.run(scenario())
    assert [row.request_id for row in stored(database)] == ["req-10"]
    losses = stored(database, "trace_loss")
    assert len(losses) == 1 and losses[0].project_id == project_id
    assert losses[0].trace == {"dropped": {"persist_failed": 3}, "total": 3}
    # Recorded once: a later batch does not report the same loss again.
    assert pipeline.persisted == 1 and pipeline._unrecorded == {}


def test_shutdown_drains_within_the_deadline_and_counts_the_rest(
    database: Database, project_id: str
) -> None:
    def never(_events: list[dict[str, Any]]) -> None:
        threading.Event().wait(1.0)

    async def scenario() -> TracePipeline:
        pipeline = TracePipeline(database, queue_size=500, persist=never)
        pipeline.start()
        for n in range(300):
            pipeline.offer(event(project_id, n))
        started = asyncio.get_running_loop().time()
        await pipeline.drain(0.3)
        assert asyncio.get_running_loop().time() - started < 3
        return pipeline

    pipeline = asyncio.run(scenario())
    state = pipeline.describe()
    assert state["persisted"] + state["dropped"]["shutdown"] == 300
    assert state["dropped"]["shutdown"] >= 200 and state["queue_depth"] == 0
    assert state["writer"] == "stopped"
    identities(pipeline)
    # After shutdown nothing is accepted, and nothing claims to be.
    assert pipeline.offer(event(project_id, 999)) == "dropped"
    identities(pipeline)


def test_the_writer_is_restarted_if_it_stops_unexpectedly(
    database: Database, project_id: str
) -> None:
    async def scenario() -> TracePipeline:
        pipeline = TracePipeline(database, queue_size=10)
        original = pipeline._next_batch
        crashes = [True]

        async def crashing(queue: Any) -> Any:
            if crashes:
                crashes.pop()
                raise RuntimeError("writer bug")
            return await original(queue)

        pipeline._next_batch = crashing  # type: ignore[method-assign]
        pipeline.start()
        pipeline.offer(event(project_id, 1))
        deadline = asyncio.get_running_loop().time() + 20
        while (pipeline.persisted < 1  # noqa: ASYNC110 - bounded poll in a test
               and asyncio.get_running_loop().time() < deadline):
            await asyncio.sleep(0.05)
        assert pipeline.restarts == 1                         # it stopped once, and came back
        assert pipeline.describe()["writer"] == "running"
        await pipeline.drain(5)
        return pipeline

    pipeline = asyncio.run(scenario())
    assert pipeline.persisted == 1 and len(stored(database)) == 1


def test_each_trace_is_capped(project_id: str) -> None:
    small = event(project_id, trace={"rules": [1, 2, 3]})
    assert cap(small) is small
    big = event(project_id, trace={
        "legacy_request": {"body": "a" * 50_000}, "upstream_request": {"body": "b" * 100},
        "upstream_response": {"body": "c" * 50_000}, "auth": {"inbound": "verified"}})
    capped = cap(big)
    size = len(json.dumps(capped["trace"]))
    assert size <= MAX_TRACE_BYTES and capped["trace"]["truncated"] is True
    assert capped["trace"]["auth"] == {"inbound": "verified"}
    assert capped["trace"]["upstream_request"] == {"body": "b" * 100}   # small parts are kept
    huge = cap(event(project_id, trace={"other": "x" * 200_000}))
    assert huge["trace"] == {"truncated": True}
    assert capped["request_id"] == big["request_id"]


def test_old_events_are_pruned_per_project(database: Database, project_id: str) -> None:
    async def scenario() -> None:
        pipeline = TracePipeline(database, queue_size=500, retention=20)
        pipeline.start()
        for n in range(60):
            pipeline.offer(event(project_id, n))
        await settle(pipeline)
        await pipeline.drain(5)
        assert pipeline.persisted == 60

    asyncio.run(scenario())
    assert len(stored(database)) == 20
