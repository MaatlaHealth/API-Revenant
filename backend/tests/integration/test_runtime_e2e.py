"""End to end: the unchanged GravePay client fails against NovaPay directly and
succeeds through the runtime, by path entry and by host entry
(requirements 6.2, 6.9 to 6.11, 7.2 to 7.4, 7.6, 7.7, 7.12, 9.2 to 9.4)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from revenant.config import Settings
from revenant.main import create_app
from tests.conftest import ADMIN_TOKEN, make_settings
from tests.integration.test_adapters_api import REVIEWED, approve
from tests.integration.test_evidence_api import base
from tests.integration.test_reconstruction_api import import_demo
from tests.live import LEGACY_KEY, NOVAPAY_TOKEN, load_simulator, run_client, serve

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
KEY = {"X-API-Key": LEGACY_KEY}
LEGACY_HOST = "api.gravepay.demo"


@dataclass
class Stack:
    """The simulator and the backend, both on real local ports."""

    settings: Settings
    simulator: Any            # the simulator module
    upstream: str
    backend: str
    project_id: str
    admin: httpx.Client

    def api(self, method: str, path: str, expect: int = 200, **kwargs: Any) -> Any:
        response = self.admin.request(method, f"{base(self.project_id)}{path}", **kwargs)
        assert response.status_code == expect, response.text
        return response.json() if response.content else None

    def events(self, **params: Any) -> list[dict]:
        """Persisted request events. Waits for the writer, which is asynchronous."""
        import time
        deadline = time.monotonic() + 10
        while True:
            health = self.api("GET", "/runtime")["health"]["traces"]
            if health["queue_depth"] == 0 and health["in_flight"] == 0 or (
                    time.monotonic() > deadline):
                break
            time.sleep(0.05)
        return self.api("GET", "/runtime/events", params={"limit": 200, **params})["items"]

    def sandbox(self) -> dict:
        return httpx.get(f"{self.upstream}/__sandbox/default/state").json()


def prepare(admin: Any, project_id: str, upstream: str, *, start: bool = True) -> None:
    """Drive the whole workflow through the management API, as a user would."""
    def call(method: str, path: str, expect: int, **kwargs: Any) -> Any:
        response = admin.request(method, f"{base(project_id)}{path}", headers=ADMIN, **kwargs)
        assert response.status_code == expect, response.text
        return response

    assert admin.patch(base(project_id), headers=ADMIN,
                       json={"replacement_base_url": upstream}).status_code == 200
    import_demo(admin, ADMIN, project_id)
    call("POST", "/reconstructions", 201)
    approve(admin, ADMIN, project_id, REVIEWED)
    call("POST", "/adapters", 201)
    call("PUT", "/credentials/outbound/replacement_credential", 200,
         json={"value": NOVAPAY_TOKEN})
    call("POST", "/credentials/inbound", 201, json={"label": "legacy client", "value": LEGACY_KEY})
    call("PATCH", "/runtime", 200, json={"legacy_hosts": [LEGACY_HOST]})
    if start:
        call("POST", "/runtime/start", 200)


def runtime_settings(url: str, upstream: str, **overrides: Any) -> Settings:
    port = upstream.rsplit(":", 1)[1]
    return make_settings(url, allow_private_upstreams=True, upstream_allowed_ports=port,
                         public_hosts="127.0.0.1,localhost,testserver", **overrides)


@pytest.fixture
def stack(migrated_url: str, project_id: str) -> Iterator[Stack]:
    simulator = load_simulator()
    with serve(simulator.app) as upstream:
        settings = runtime_settings(migrated_url, upstream)
        app = create_app(settings, supervised=False)
        with serve(app.state.dispatcher) as backend, httpx.Client(
                base_url=backend, headers=ADMIN, timeout=60) as admin:
            prepare(admin, project_id, upstream)
            yield Stack(settings, simulator, upstream, backend, project_id, admin)


# ============================================================================= the demonstration


def test_the_unchanged_client_fails_directly_and_succeeds_through_the_runtime(
    stack: Stack
) -> None:
    before = run_client(stack.upstream)
    assert before.returncode == 1 and "FAILED" in before.stdout
    created_before = stack.sandbox()["side_effects"]["payments_created"]

    after = run_client(f"{stack.backend}/runtime/gravepay")
    assert after.returncode == 0, after.stdout + after.stderr
    assert "OK: GravePay behaved as this client expects." in after.stdout
    assert "4. invalid currency rejected with code invalid_currency" in after.stdout
    assert "3. listed page 2: P-1003, P-1004" in after.stdout
    # The same client, byte for byte: the checksum test covers the file itself.
    effects = stack.sandbox()["side_effects"]
    assert effects["payments_created"] == created_before + 1
    assert effects["refunds_created"] == 1

    events = [e for e in stack.events(kind="request")]
    assert [(e["method"], e["legacy_path"], e["legacy_status"], e["outcome"])
            for e in reversed(events)] == [
        ("POST", "/v1/charges", 201, "ok"),
        ("GET", "/v1/charges/P-2001", 200, "ok"),
        ("GET", "/v1/charges", 200, "ok"),
        ("POST", "/v1/charges", 400, "ok"),
        ("POST", "/v1/charges/P-2001/refund", 201, "ok"),
    ]
    assert all(e["entry"] == "path" for e in events)
    assert events[2]["legacy_query"] == "page=2&limit=2"
    assert events[2]["upstream_path"] == "/payments"


def test_host_entry_serves_the_client_under_its_original_hostname(stack: Stack) -> None:
    """The client's own hostname reaches the runtime; no path prefix, no override in
    the request. (Docker DNS delivers the connection; here the Host header does.)"""
    charge = httpx.post(f"{stack.backend}/v1/charges", headers={**KEY, "Host": LEGACY_HOST},
                        json={"customer": "C9", "amount": 70, "currency": "EUR"})
    assert charge.status_code == 201, charge.text
    body = charge.json()
    assert body == {"id": "P-2001", "customer": "C9", "amount": 70, "currency": "EUR",
                    "status": "succeeded", "created": "2025-03-14T09:21:07Z"}
    assert charge.headers["x-revenant-adapter"] == "1"
    assert charge.headers["x-revenant-trace"] == "queued"
    request_id = charge.headers["x-revenant-request-id"]
    for variant in ("API.GravePay.Demo", "api.gravepay.demo.", "api.gravepay.demo:8000"):
        again = httpx.get(f"{stack.backend}/v1/charges/P-2001", headers={**KEY, "Host": variant})
        assert again.status_code == 200 and again.json()["id"] == "P-2001"

    stored = None
    stack.events()
    stored = stack.api("GET", f"/runtime/events/{request_id}")
    assert stored["entry"] == "host" and stored["original_host"] == LEGACY_HOST
    assert stored["route_id"] == "post-v1-charges" and stored["upstream_status"] == 201
    trace = stored["trace"]
    assert trace["auth"] == {"inbound": "verified", "key_label": "legacy client",
                             "outbound": "bearer attached", "credential_version": 1}
    assert trace["upstream_request"]["body"] == {
        "customer_id": "C9", "transaction": {"value": 70, "currency_code": "EUR"}}
    # The snapshot was taken before the credential was attached.
    assert trace["upstream_request"]["headers"] == {}
    blob = json.dumps(stored)
    assert LEGACY_KEY not in blob and NOVAPAY_TOKEN not in blob


def test_the_management_api_is_not_reachable_on_a_legacy_hostname(stack: Stack) -> None:
    host = {"Host": LEGACY_HOST}
    for path in ("/api/v1/projects", "/api/v1/docs", "/api/v1/openapi.json", "/healthz",
                 "/readyz", "/runtime/gravepay/v1/charges/P-1001"):
        for headers in ({**host, **ADMIN}, {**host, **ADMIN, **KEY}):
            response = httpx.get(f"{stack.backend}{path}", headers=headers)
            # An ordinary legacy path: the adapter has no such route.
            assert response.status_code in (401, 404), path
            assert "projects" not in response.text and "openapi" not in response.text
    health = httpx.get(f"{stack.backend}/__revenant/health", headers=host)
    assert health.status_code == 200 and health.json()["status"] == "ok"
    # The admin token is no substitute for a legacy key, and the reverse.
    assert httpx.get(f"{stack.backend}/v1/charges/P-1001",
                     headers={**host, **ADMIN}).status_code == 401
    assert httpx.get(f"{stack.backend}/api/v1/projects", headers=KEY).status_code == 401


def test_unknown_and_malformed_hosts_are_refused(stack: Stack) -> None:
    def status(host: str, path: str = "/api/v1/projects") -> int:
        return httpx.get(f"{stack.backend}{path}", headers={"Host": host, **ADMIN}).status_code

    assert status("127.0.0.1") == 200 and status("LOCALHOST:8000") == 200
    for host in ("evil.example", "gravepay.demo", "x.api.gravepay.demo",
                 "api.gravepay.demo.evil.example", "api-gravepay.demo", "10.0.0.1"):
        assert status(host) == 421, host
        assert status(host, "/v1/charges/P-1001") == 421, host
    # Probes are answered for any well-formed host; nothing else is.
    assert status("evil.example", "/healthz") == 200
    for host in ("api_gravepay.demo", "api.gravepay.demo:99999", "a..b", "-a.example",
                 "api.gravepay.demo/x", "user@api.gravepay.demo", "api.gravepay.demo:"):
        assert status(host) == 400, host
        assert status(host, "/healthz") == 400, host


def test_forwarded_host_headers_are_ignored(stack: Stack) -> None:
    for name in ("X-Forwarded-Host", "Forwarded", "X-Original-Host", "X-Host",
                 "X-Forwarded-Server"):
        value = f"host={LEGACY_HOST}" if name == "Forwarded" else LEGACY_HOST
        # On a management host the header does not turn the request into a runtime request.
        response = httpx.get(f"{stack.backend}/v1/charges/P-1001", headers={**KEY, name: value})
        assert response.status_code == 404 and "revenant_error" not in response.text, name
        # And an unknown host stays refused whatever the header claims.
        refused = httpx.get(f"{stack.backend}/v1/charges/P-1001",
                            headers={**KEY, "Host": "evil.example", name: value})
        assert refused.status_code == 421, name


# ============================================================================= authentication


def test_legacy_credentials_are_required_and_never_forwarded(stack: Stack) -> None:
    url = f"{stack.backend}/runtime/gravepay/v1/charges/P-1001"
    before = stack.sandbox()["requests"]
    for headers in ({}, {"X-API-Key": "wrong-key-value-123"}, {"X-API-Key": ""},
                    {"Authorization": f"Bearer {LEGACY_KEY}"}, ADMIN,
                    {"X-API-Key": NOVAPAY_TOKEN}):
        response = httpx.get(url, headers=headers)
        assert response.status_code == 401, headers
        assert response.json() == {"revenant_error": "auth_rejected"}
    # Nothing reached the replacement for any rejected request.
    assert stack.sandbox()["requests"] == before
    ok = httpx.get(url, headers=KEY)
    assert ok.status_code == 200
    # The simulator refuses any request that carries a GravePay key, so a
    # forwarded key would have failed here.
    assert stack.sandbox()["requests"] == before + 1
    rejected = stack.events(outcome="auth_rejected")
    assert len(rejected) == 6 and all(e["upstream_status"] is None for e in rejected)


def test_a_rejected_replacement_credential_is_a_502_not_a_401(stack: Stack) -> None:
    stack.api("PUT", "/credentials/outbound/replacement_credential",
              json={"value": "a-token-novapay-rejects"})
    response = httpx.get(f"{stack.backend}/runtime/gravepay/v1/charges/P-1001", headers=KEY)
    assert response.status_code == 502
    assert response.json() == {"revenant_error": "upstream_auth_failed"}
    health = stack.api("GET", "/runtime")["health"]
    assert health["upstream"]["last_outcome"] == "auth_failed"
    # Rolling back restores service without a restart.
    stack.api("POST", "/credentials/outbound/replacement_credential/rollback")
    assert httpx.get(f"{stack.backend}/runtime/gravepay/v1/charges/P-1001",
                     headers=KEY).status_code == 200
    events = stack.events(kind="lifecycle")
    assert [e["outcome"] for e in events][:2] == ["reloaded", "reloaded"]


def test_keys_can_be_added_and_revoked_without_a_restart(stack: Stack) -> None:
    url = f"{stack.backend}/runtime/gravepay/v1/charges/P-1001"
    second = "second-legacy-client-key-0002"
    assert httpx.get(url, headers={"X-API-Key": second}).status_code == 401
    created = stack.api("POST", "/credentials/inbound", expect=201,
                        json={"label": "second client", "value": second})
    assert httpx.get(url, headers={"X-API-Key": second}).status_code == 200
    assert httpx.get(url, headers=KEY).status_code == 200  # several keys at once
    first = next(k for k in stack.api("GET", "/credentials/inbound")["items"]
                 if k["label"] == "legacy client")
    stack.api("POST", f"/credentials/inbound/{first['id']}/revoke")
    assert httpx.get(url, headers=KEY).status_code == 401
    assert httpx.get(url, headers={"X-API-Key": second}).status_code == 200
    # The last active key of a running runtime cannot be revoked.
    refused = stack.admin.post(f"{base(stack.project_id)}/credentials/inbound/"
                               f"{created['id']}/revoke")
    assert refused.status_code == 409
    assert httpx.get(url, headers={"X-API-Key": second}).status_code == 200


# ============================================================================= behaviour


def test_errors_routes_and_runtime_generated_statuses(stack: Stack) -> None:
    root = f"{stack.backend}/runtime/gravepay"
    missing = httpx.get(f"{root}/v1/charges/P-9999", headers=KEY)
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "charge_not_found",
                                        "message": "No such resource: P-9999."}}
    assert "x-revenant-unmapped" not in missing.headers

    # A replacement error no approved rule covers: status kept, nothing invented.
    refund = {"amount": 99999}
    unmapped = httpx.post(f"{root}/v1/charges/P-1005/refund", headers=KEY, json=refund)
    assert unmapped.status_code == 409 and unmapped.content == b""
    assert unmapped.headers["x-revenant-unmapped"] == "error"

    assert httpx.delete(f"{root}/v1/charges/P-1001", headers=KEY).json() == {
        "revenant_error": "no_route"}
    assert httpx.get(f"{root}/v1/charges/P-1001/events", headers=KEY).status_code == 404
    bad_page = httpx.get(f"{root}/v1/charges?page=0", headers=KEY)
    assert bad_page.status_code == 400 and bad_page.json() == {"revenant_error":
                                                              "transform_error"}
    not_json = httpx.post(f"{root}/v1/charges", headers={**KEY, "Content-Type": "text/plain"},
                          content=b"customer=C1")
    assert not_json.status_code == 400
    too_big = httpx.post(f"{root}/v1/charges", headers=KEY,
                         content=b'{"customer": "' + b"x" * 1_100_000 + b'"}')
    assert too_big.status_code == 413
    assert httpx.get(f"{stack.backend}/runtime/no-such-project/v1/charges",
                     headers=KEY).status_code == 404
    outcomes = {e["outcome"] for e in stack.events(kind="request")}
    assert outcomes >= {"ok", "unmapped_error", "no_route", "transform_error"}


def test_path_values_are_decoded_once_and_cannot_escape_their_segment(stack: Stack) -> None:
    root = f"{stack.backend}/runtime/gravepay/v1/charges"
    plain = httpx.get(f"{root}/P-1001%3Fx%3D1", headers=KEY)
    assert plain.status_code == 404
    assert plain.json()["error"] == {"code": "charge_not_found",
                                     "message": "No such resource: P-1001?x=1."}
    # An encoded slash stays inside its segment: it is sent upstream encoded, as one
    # path parameter, and can never become extra path segments chosen by the client.
    for sent in ("P-1001%2F..%2F..%2F__sandbox%2Fdefault%2Freset", "..%2FP-1001"):
        response = httpx.get(f"{root}/{sent}", headers=KEY)
        assert response.status_code == 404, sent
    events = stack.events(kind="request")
    assert [event["upstream_path"] for event in reversed(events)] == [
        "/payments/P-1001%3Fx%3D1",
        "/payments/P-1001%2F..%2F..%2F__sandbox%2Fdefault%2Freset",
        "/payments/..%2FP-1001"]
    assert all(event["route_id"] == "get-v1-charges-id" for event in events)
    state = stack.sandbox()
    assert state["payments"] == 5 and state["side_effects"]["receipts_issued"] == 0


def test_the_receipt_route_reaches_the_side_effecting_get(stack: Stack) -> None:
    receipt = httpx.get(f"{stack.backend}/runtime/gravepay/v1/charges/P-1002/receipt",
                        headers=KEY)
    assert receipt.json() == {"charge": "P-1002", "receipt_number": "RCPT-1002",
                              "issued": "2025-03-02T09:31:00Z"}
    assert stack.sandbox()["side_effects"]["receipts_issued"] == 1


# ============================================================================= control


def test_stop_start_and_health(stack: Stack) -> None:
    url = f"{stack.backend}/runtime/gravepay/v1/charges/P-1001"
    described = stack.api("GET", "/runtime")
    assert (described["desired_state"], described["status"], described["adapter_version"]) == (
        "running", "running", 1)
    assert described["legacy_hosts"] == [LEGACY_HOST]
    health = httpx.get(f"{stack.backend}/runtime/gravepay/__revenant/health").json()
    assert health["status"] == "ok" and health["adapter"] == {
        "loaded": True, "version": 1, "generation": 1}
    assert health["lease"]["state"] == "held" and health["lease"]["epoch"] == 1
    assert health["requests"]["accepting"] is True
    assert health["traces"]["writer"] == "running"
    assert LEGACY_KEY not in json.dumps(health) and NOVAPAY_TOKEN not in json.dumps(health)

    stopped = stack.api("POST", "/runtime/stop")
    assert (stopped["desired_state"], stopped["status"]) == ("stopped", "stopped")
    for headers in (KEY, {**KEY, "Host": LEGACY_HOST}):
        path = url if "Host" not in headers else f"{stack.backend}/v1/charges/P-1001"
        response = httpx.get(path, headers=headers)
        assert response.status_code == 503
        assert response.headers["x-revenant-runtime"] == "stopped"
    assert httpx.get(f"{stack.backend}/runtime/gravepay/__revenant/health").json()[
        "status"] == "stopped"
    # A project whose runtime is running cannot be deleted; a stopped one can be started again.
    started = stack.api("POST", "/runtime/start", json={"adapter_version": 1})
    assert started["generation"] == 3 and started["status"] == "running"
    assert httpx.get(url, headers=KEY).status_code == 200
    assert stack.admin.delete(base(stack.project_id)).status_code == 409
    assert [e["outcome"] for e in stack.events(kind="lifecycle")] == [
        "started", "stopped", "started"]


def test_start_is_refused_until_the_runtime_is_ready(migrated_url: str, project_id: str) -> None:
    simulator = load_simulator()
    with serve(simulator.app) as upstream:
        app = create_app(runtime_settings(migrated_url, upstream), supervised=False)
        with TestClient(app) as client:
            def start() -> Any:
                return client.post(f"{base(project_id)}/runtime/start", headers=ADMIN)

            assert start().json()["error"]["details"][0]["issue"] == "no_adapter"
            client.patch(base(project_id), headers=ADMIN,
                         json={"replacement_base_url": upstream})
            import_demo(client, ADMIN, project_id)
            client.post(f"{base(project_id)}/reconstructions", headers=ADMIN)
            approve(client, ADMIN, project_id, REVIEWED)
            assert client.post(f"{base(project_id)}/adapters", headers=ADMIN).status_code == 201
            refused = start()
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "runtime_not_ready"
            assert refused.json()["error"]["details"][0]["field"] == "credential_unavailable"
            client.put(f"{base(project_id)}/credentials/outbound/replacement_credential",
                       headers=ADMIN, json={"value": NOVAPAY_TOKEN})
            assert start().json()["error"]["details"][0]["field"] == "no_active_inbound_key"
            # Nothing was half-started by the refusals.
            runtime = client.get(f"{base(project_id)}/runtime", headers=ADMIN).json()
            assert (runtime["desired_state"], runtime["status"]) == ("stopped", "stopped")
            assert client.get("/runtime/gravepay/v1/charges/P-1001",
                              headers=KEY).status_code == 503
            client.post(f"{base(project_id)}/credentials/inbound", headers=ADMIN,
                        json={"label": "client", "value": LEGACY_KEY})
            assert start().status_code == 200
            assert client.get("/runtime/gravepay/v1/charges/P-1001",
                              headers=KEY).status_code == 200


def test_legacy_host_registration_is_strict(stack: Stack) -> None:
    def register(hosts: list[str], expect: int) -> Any:
        return stack.api("PATCH", "/runtime", expect=expect, json={"legacy_hosts": hosts})

    for bad in ("*.gravepay.demo", "10.1.2.3", "[::1]", "localhost", "127.0.0.1",
                "api_gravepay.demo", "a b.example", "api.gravepay.demo/x", ""):
        assert register([bad], 400)["error"]["code"] == "validation_failed", bad
    # Stored in normalised form, whatever spelling was sent.
    assert register(["API.GravePay.Demo.:8000", "bücher.example"], 200)["legacy_hosts"] == [
        "api.gravepay.demo", "xn--bcher-kva.example"]
    other = stack.admin.post("/api/v1/projects", json={
        "name": "Other", "legacy_name": "A", "replacement_name": "B"}).json()["id"]
    for variant in ("api.gravepay.demo", "API.GRAVEPAY.DEMO", "api.gravepay.demo.",
                    "api.gravepay.demo:443", "BÜCHER.example", "xn--bcher-kva.example"):
        response = stack.admin.patch(f"{base(other)}/runtime", json={"legacy_hosts": [variant]})
        assert response.status_code == 409, variant
    # Removing a host stops routing it.
    register([], 200)
    assert httpx.get(f"{stack.backend}/v1/charges/P-1001",
                     headers={**KEY, "Host": LEGACY_HOST}).status_code == 421


def test_runtime_routes_require_the_admin_token(stack: Stack) -> None:
    for method, path in (("GET", "/runtime"), ("POST", "/runtime/start"),
                         ("POST", "/runtime/stop"), ("PATCH", "/runtime"),
                         ("GET", "/runtime/events"), ("GET", "/runtime/events/x")):
        for headers in ({}, KEY, {"Authorization": f"Bearer {LEGACY_KEY}"}):
            response = httpx.request(method, f"{stack.backend}{base(stack.project_id)}{path}",
                                     headers=headers, json={})
            assert response.status_code == 401, path


def test_demo_files_are_where_the_documentation_says() -> None:
    root = Path(__file__).resolve().parents[3] / "demo"
    for relative in ("novapay/simulator/app.py", "legacy-client/gravepay_client.py",
                     "legacy-client/SHA256"):
        assert (root / relative).is_file(), relative
