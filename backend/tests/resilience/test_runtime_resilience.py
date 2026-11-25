"""Request lifetime, lease checks on the request path, concurrency, restart
recovery and query-parameter credentials, against real local servers
(requirements 6.13 to 6.17, 7.4, 7.5, 7.9, 7.11, 7.32 to 7.35, 11.8, 11.9)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import text

from revenant.db.session import Database
from revenant.main import create_app
from tests.integration.test_evidence_api import base
from tests.integration.test_reconstruction_api import by_key
from tests.integration.test_runtime_e2e import (
    ADMIN,
    KEY,
    LEGACY_HOST,
    Stack,
    prepare,
    runtime_settings,
)
from tests.live import LEGACY_KEY, NOVAPAY_TOKEN, load_simulator, serve

Handler = Callable[[httpx.Request], Any]


@pytest.fixture
def upstream() -> Iterator[tuple[Any, str]]:
    simulator = load_simulator()
    with serve(simulator.app) as url:
        yield simulator, url


def build(migrated_url: str, project_id: str, upstream: tuple[Any, str], *, start: bool = True,
          **overrides: Any) -> Any:
    """An application with the demonstration adapter compiled (and started)."""
    simulator, url = upstream
    settings = runtime_settings(migrated_url, url, **overrides)
    app = create_app(settings, supervised=False)
    return app, settings, simulator, url


@pytest.fixture
def fast(migrated_url: str, project_id: str, upstream: tuple[Any, str]) -> Iterator[Stack]:
    """A stack whose request lifetime is one second, so deadlines can be tested."""
    simulator, url = upstream
    settings = runtime_settings(migrated_url, url, request_lifetime_seconds=1.0,
                                upstream_timeout_seconds=1.0)
    app = create_app(settings, supervised=False)
    with serve(app.state.dispatcher) as backend, httpx.Client(
            base_url=backend, headers=ADMIN, timeout=60) as admin:
        prepare(admin, project_id, url)
        stack = Stack(settings, simulator, url, backend, project_id, admin)
        stack.app = app  # type: ignore[attr-defined]
        yield stack


def replace_upstream(stack: Stack, handler: Handler) -> list[httpx.Request]:
    """Swap the outbound client for one that calls ``handler``; returns what it received."""
    seen: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result = handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    stack.app.state.proxy.client = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(record))
    return seen


def get(stack: Stack, path: str = "/v1/charges/P-1001", **kwargs: Any) -> httpx.Response:
    return httpx.get(f"{stack.backend}/runtime/gravepay{path}", headers=KEY, timeout=30,
                     **kwargs)


PAYMENT = {"payment_id": "P-1001", "customer_id": "C101", "state": "COMPLETED",
           "transaction": {"value": 250, "currency_code": "ZAR"}, "created_at": 1740816000}


# ============================================================================= lifetime


def test_a_slow_upstream_ends_at_the_request_lifetime_with_504(fast: Stack) -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=PAYMENT)

    replace_upstream(fast, slow)
    started = time.monotonic()
    response = get(fast)
    elapsed = time.monotonic() - started
    assert response.status_code == 504
    assert response.json() == {"revenant_error": "request_deadline_exceeded"}
    # One lifetime (1 s), not the upstream's five seconds. The upper bound is
    # generous because a shared CI machine can be slow to schedule.
    assert 0.8 < elapsed < 4.5
    events = fast.events(outcome="request_deadline_exceeded")
    assert len(events) == 1 and events[0]["legacy_status"] == 504
    assert fast.api("GET", "/runtime")["health"]["requests"]["in_flight"] == 0


def test_a_body_that_never_arrives_ends_with_408(fast: Stack) -> None:
    address = urlsplit(fast.backend)
    started = time.monotonic()
    with socket.create_connection((address.hostname, address.port), timeout=10) as raw:
        raw.sendall(b"POST /runtime/gravepay/v1/charges HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nX-API-Key: " + LEGACY_KEY.encode() + b"\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 500\r\n\r\n"
                    b'{"customer": "C1"')          # ...and then nothing more
        elapsed = None
        reply = ""
        while "request_timeout" not in reply:      # headers and body may arrive separately
            chunk = raw.recv(4096)
            if elapsed is None:
                elapsed = time.monotonic() - started
            if not chunk:
                break
            reply += chunk.decode("latin-1")
    assert elapsed is not None
    assert reply.startswith("HTTP/1.1 408"), reply[:80]
    assert "request_timeout" in reply and 0.8 < elapsed < 10
    assert fast.sandbox()["requests"] == 0           # nothing was sent upstream
    events = fast.events(outcome="request_timeout")
    assert len(events) == 1 and events[0]["upstream_status"] is None


def test_upstream_failures_map_to_502_and_504(fast: Stack) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to https://up.example/?token=SECRETVALUE")

    replace_upstream(fast, refuse)
    refused = get(fast)
    assert refused.status_code == 502 and refused.json() == {"revenant_error": "upstream_error"}

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    replace_upstream(fast, timeout)
    timed_out = get(fast)
    assert timed_out.status_code == 504 and timed_out.json() == {
        "revenant_error": "upstream_error"}

    replace_upstream(fast, lambda _r: httpx.Response(200, content=b"<html>not json</html>"))
    assert get(fast).status_code == 502
    replace_upstream(fast, lambda _r: httpx.Response(200, json={"unexpected": "shape"}))
    assert get(fast).status_code == 502              # required legacy fields cannot be built
    health = fast.api("GET", "/runtime")["health"]
    assert health["upstream"]["last_outcome"] == "ok"
    stored = json.dumps(fast.events())
    # The exception text, which could carry a URL with a credential, is never recorded.
    assert "SECRETVALUE" not in stored and "ConnectError" in json.dumps(
        [fast.api("GET", f"/runtime/events/{e['id']}") for e in fast.events(
            outcome="upstream_error")])


# ============================================================================= lease on the path


def test_the_second_lease_check_stops_a_request_before_anything_is_sent(fast: Stack) -> None:
    seen = replace_upstream(fast, lambda _r: httpx.Response(200, json=PAYMENT))
    assert get(fast).status_code == 200 and len(seen) == 1
    lease = fast.app.state.lease  # type: ignore[attr-defined]
    original = lease.may_send
    lease.may_send = lambda: False               # ownership lost after admission
    try:
        response = get(fast)
    finally:
        lease.may_send = original
    assert response.status_code == 503
    assert response.headers["x-revenant-runtime"] == "lease_expired"
    assert len(seen) == 1                        # the upstream received nothing


@pytest.mark.parametrize(("state", "header"), [
    ("standby", "standby"), ("handover", "handover"), ("superseded", "superseded"),
    ("fenced", "superseded"),
])
def test_nothing_is_admitted_without_the_lease(fast: Stack, state: str, header: str) -> None:
    seen = replace_upstream(fast, lambda _r: httpx.Response(200, json=PAYMENT))
    lease = fast.app.state.lease  # type: ignore[attr-defined]
    held = lease.state
    lease.state = state
    try:
        for url in (f"{fast.backend}/runtime/gravepay/v1/charges/P-1001",
                    f"{fast.backend}/v1/charges/P-1001"):
            headers = KEY if "runtime" in url else {**KEY, "Host": LEGACY_HOST}
            response = httpx.get(url, headers=headers)
            assert response.status_code == 503
            assert response.headers["x-revenant-runtime"] == header
            assert ("retry-after" in response.headers) is (header == "handover")
        health = httpx.get(f"{fast.backend}/runtime/gravepay/__revenant/health").json()
        assert health["status"] == "not_serving" and health["requests"]["accepting"] is False
        assert httpx.get(f"{fast.backend}/readyz").status_code == 503
    finally:
        lease.state = held
    assert seen == [] and get(fast).status_code == 200


def test_requests_in_progress_are_cancelled_when_ownership_is_lost(fast: Stack) -> None:
    entered = threading.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json=PAYMENT)

    replace_upstream(fast, hang)
    fast.app.state.proxy.lifetime = 20           # type: ignore[attr-defined]
    results: list[httpx.Response] = []
    threads = [threading.Thread(target=lambda: results.append(get(fast))) for _ in range(3)]
    for thread in threads:
        thread.start()
    assert entered.wait(10)
    time.sleep(0.3)
    started = time.monotonic()
    fast.app.state.proxy.cancel_all("lease_lost")   # type: ignore[attr-defined]
    for thread in threads:
        thread.join(10)
    assert time.monotonic() - started < 10       # at once, not at the 20 s deadline
    assert [r.status_code for r in results] == [503, 503, 503]
    assert all(r.headers["x-revenant-runtime"] == "superseded" for r in results)
    assert fast.app.state.proxy.in_flight == 0   # type: ignore[attr-defined]
    assert len(fast.events(outcome="lease_lost")) == 3


def test_concurrent_requests_do_not_share_data(fast: Stack) -> None:
    async def echo(request: httpx.Request) -> httpx.Response:
        payment_id = request.url.path.rsplit("/", 1)[1]
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={**PAYMENT, "payment_id": payment_id,
                                         "customer_id": f"C-{payment_id}"})

    replace_upstream(fast, echo)
    results: dict[str, dict] = {}

    def call(number: int) -> None:
        results[f"P-{number}"] = get(fast, f"/v1/charges/P-{number}").json()

    threads = [threading.Thread(target=call, args=(n,)) for n in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(results) == 40
    for wanted, body in results.items():
        assert (body["id"], body["customer"]) == (wanted, f"C-{wanted}")


def test_the_concurrency_cap_refuses_the_excess(fast: Stack) -> None:
    entered = threading.Semaphore(0)
    release = threading.Event()

    async def hold(_request: httpx.Request) -> httpx.Response:
        entered.release()
        while not release.is_set():  # noqa: ASYNC110 - held until the test lets go
            await asyncio.sleep(0.01)
        return httpx.Response(200, json=PAYMENT)

    replace_upstream(fast, hold)
    fast.app.state.proxy.lifetime = 20       # type: ignore[attr-defined]
    fast.settings.runtime_max_concurrency = 2
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(get(fast).status_code))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    assert entered.acquire(timeout=10) and entered.acquire(timeout=10)
    try:
        excess = get(fast)
        assert excess.status_code == 503 and excess.headers["retry-after"] == "1"
        assert excess.json() == {"revenant_error": "concurrency_limit"}
    finally:
        release.set()
    for thread in threads:
        thread.join(10)
    assert results == [200, 200]
    assert get(fast).status_code == 200      # capacity is free again


# ============================================================================= recovery


def test_a_restart_restores_the_runtime_on_its_pinned_version(
    migrated_url: str, project_id: str, upstream: tuple[Any, str], database: Database
) -> None:
    _simulator, url = upstream
    settings = runtime_settings(migrated_url, url)
    with serve(create_app(settings, supervised=False).state.dispatcher) as first, httpx.Client(
            base_url=first, headers=ADMIN, timeout=60) as admin:
        prepare(admin, project_id, url)
        # A newer adapter exists but was never started.
        assert admin.post(f"{base(project_id)}/adapters").json()["version"] == 2
        assert httpx.get(f"{first}/runtime/gravepay/v1/charges/P-1001",
                         headers=KEY).headers["x-revenant-adapter"] == "1"
    with database.session() as session:
        released, epoch = session.execute(text(
            "SELECT released, epoch FROM instance_lease")).one()
    assert bool(released) is True and epoch == 1     # a graceful stop releases the lease

    # A new process. No management call is made before the first runtime request.
    with serve(create_app(settings, supervised=False).state.dispatcher) as second:
        for target, headers in ((f"{second}/runtime/gravepay/v1/charges/P-1001", KEY),
                                (f"{second}/v1/charges/P-1001", {**KEY, "Host": LEGACY_HOST})):
            response = httpx.get(target, headers=headers)
            assert response.status_code == 200 and response.json()["id"] == "P-1001"
            assert response.headers["x-revenant-adapter"] == "1"    # pinned, not "latest"
        ready = httpx.get(f"{second}/readyz").json()
        assert ready["status"] == "ready" and ready["checks"]["recovery"] == "done"
        runtime = httpx.get(f"{second}{base(project_id)}/runtime", headers=ADMIN).json()
        assert (runtime["status"], runtime["adapter_version"]) == ("running", 1)
        assert runtime["health"]["lease"]["epoch"] == 2
        events = httpx.get(f"{second}{base(project_id)}/runtime/events",
                           headers=ADMIN, params={"kind": "lifecycle"}).json()["items"]
        assert events[0]["outcome"] == "recovered"
        assert events[0]["trace"] == {"adapter_version": 1, "generation": 1}


def test_one_runtime_that_cannot_recover_does_not_stop_the_others(
    migrated_url: str, project_id: str, upstream: tuple[Any, str], database: Database
) -> None:
    _simulator, url = upstream
    settings = runtime_settings(migrated_url, url)
    with serve(create_app(settings, supervised=False).state.dispatcher) as first, httpx.Client(
            base_url=first, headers=ADMIN, timeout=60) as admin:
        prepare(admin, project_id, url)
        broken = admin.post("/api/v1/projects", json={
            "name": "Broken", "slug": "broken", "legacy_name": "GravePay",
            "replacement_name": "NovaPay"}).json()["id"]
        prepare(admin, broken, url, start=False, hosts=[])
        assert admin.post(f"{base(broken)}/runtime/start").status_code == 200
        credentials = admin.post("/api/v1/projects", json={
            "name": "No credential", "slug": "nocred", "legacy_name": "GravePay",
            "replacement_name": "NovaPay"}).json()["id"]
        prepare(admin, credentials, url, start=False, hosts=[])
        assert admin.post(f"{base(credentials)}/runtime/start").status_code == 200
    with database.session() as session:
        # A corrupted adapter row, and a credential that is no longer usable.
        session.execute(text("UPDATE adapter_versions SET document = :doc WHERE project_id = :p"),
                        {"doc": '{"schema_version": "9"}', "p": broken})
        session.execute(text("UPDATE outbound_credentials SET status = 'retired' "
                             "WHERE project_id = :p"), {"p": credentials})

    with serve(create_app(settings, supervised=False).state.dispatcher) as second:
        assert httpx.get(f"{second}/runtime/gravepay/v1/charges/P-1001",
                         headers=KEY).status_code == 200
        for slug, project, reason in (("broken", broken, "adapter_invalid"),
                                      ("nocred", credentials, "credential_unavailable")):
            response = httpx.get(f"{second}/runtime/{slug}/v1/charges/P-1001", headers=KEY)
            assert response.status_code == 503
            assert response.headers["x-revenant-runtime"] == "failed"
            runtime = httpx.get(f"{second}{base(project)}/runtime", headers=ADMIN).json()
            # Desired state is kept, so a retry is possible once the cause is fixed.
            assert (runtime["desired_state"], runtime["status"]) == ("running", "failed")
            assert runtime["last_error"].startswith(reason)
            assert runtime["health"]["status"] == "failed"
        assert httpx.get(f"{second}/readyz").status_code == 200     # the process stays up


# ============================================================================= query credentials


@pytest.fixture
def query_auth(migrated_url: str, project_id: str, upstream: tuple[Any, str]) -> Iterator[Stack]:
    """The demonstration adapter, with the legacy key carried in ``?api_key=``."""
    simulator, url = upstream
    settings = runtime_settings(migrated_url, url)
    app = create_app(settings, supervised=False)
    with serve(app.state.dispatcher) as backend, httpx.Client(
            base_url=backend, headers=ADMIN, timeout=60) as admin:
        prepare(admin, project_id, url, start=False)
        mappings = by_key(admin.get(f"{base(project_id)}/mappings",
                                    params={"limit": 200}).json()["items"])
        edited = admin.put(f"{base(project_id)}/mappings/{mappings['auth']['id']}", json={
            "transformation": {"inbound": {"type": "api_key_query", "param": "api_key"},
                               "outbound": {"type": "bearer",
                                            "secret_ref": "replacement_credential"}}}).json()
        assert admin.post(f"{base(project_id)}/mappings/{edited['id']}/approve"
                          ).status_code == 200
        assert admin.post(f"{base(project_id)}/adapters").json()["version"] == 2
        assert admin.post(f"{base(project_id)}/runtime/start").status_code == 200
        stack = Stack(settings, simulator, url, backend, project_id, admin)
        stack.app = app  # type: ignore[attr-defined]
        yield stack


def test_a_query_parameter_credential_is_removed_before_anything_else(query_auth: Stack) -> None:
    root = f"{query_auth.backend}/runtime/gravepay/v1/charges"
    listed = httpx.get(f"{root}?page=2&api_key={LEGACY_KEY}&limit=2")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["data"]] == ["P-1003", "P-1004"]
    # A header is no longer the carrier, and a missing or wrong key is refused.
    assert httpx.get(f"{root}?page=1", headers=KEY).status_code == 401
    assert httpx.get(f"{root}?page=1&api_key=wrong-key-value-12345").status_code == 401
    # An ambiguous credential is never guessed, even if both values are right.
    twice = httpx.get(f"{root}?api_key={LEGACY_KEY}&page=1&api_key={LEGACY_KEY}")
    assert twice.status_code == 401
    assert query_auth.sandbox()["requests"] == 1

    events = query_auth.events(kind="request")
    assert [e["legacy_query"] for e in reversed(events)] == [
        "page=2&limit=2", "page=1", "page=1", "page=1"]
    assert [e["query_credential_removed"] for e in reversed(events)] == [True, False, True, True]
    everything = json.dumps([query_auth.api("GET", f"/runtime/events/{e['id']}")
                             for e in events])
    assert LEGACY_KEY not in everything and "wrong-key-value-12345" not in everything
    assert NOVAPAY_TOKEN not in everything


def test_other_parameters_keep_their_bytes_and_order(query_auth: Stack) -> None:
    root = f"{query_auth.backend}/runtime/gravepay/v1/charges"
    raw = f"z=%7E1&page=1&a=b+c&&api%5Fkey={LEGACY_KEY}&limit=2&z=2"
    assert httpx.get(f"{root}?{raw}").status_code == 200
    stored = query_auth.events(kind="request")[0]
    assert stored["legacy_query"] == "z=%7E1&page=1&a=b+c&&limit=2&z=2"
    assert stored["query_credential_removed"] is True


def test_a_legacy_key_that_would_travel_upstream_is_blocked(query_auth: Stack) -> None:
    """The client also puts its key in a body field that a mapping forwards."""
    before = query_auth.sandbox()["requests"]
    response = httpx.post(
        f"{query_auth.backend}/runtime/gravepay/v1/charges?api_key={LEGACY_KEY}",
        json={"customer": LEGACY_KEY, "amount": 5, "currency": "ZAR"})
    assert response.status_code == 500
    assert response.json() == {"revenant_error": "credential_leak_blocked"}
    assert query_auth.sandbox()["requests"] == before
    stored = query_auth.events(outcome="credential_leak_blocked")
    assert len(stored) == 1
    assert LEGACY_KEY not in json.dumps(query_auth.api("GET",
                                                       f"/runtime/events/{stored[0]['id']}"))
