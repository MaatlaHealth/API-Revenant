"""The demonstration services: the NovaPay simulator and the unchanged GravePay
client (requirements 9.1, 9.2, 9.6, 9.7, 9.8). Both are fictional."""

from __future__ import annotations

import ast
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from tests.live import CLIENT, DEMO, NOVAPAY_TOKEN, SIMULATOR, load_simulator, run_client, serve

AUTH = {"Authorization": f"Bearer {NOVAPAY_TOKEN}"}


@pytest.fixture
def simulator() -> TestClient:
    return TestClient(load_simulator().app)


def recordings() -> list[dict]:
    return json.loads((DEMO / "novapay" / "recordings.json").read_text("utf-8"))["fixtures"]


def test_the_simulator_reproduces_every_recording(simulator: TestClient) -> None:
    """The recordings are evidence of NovaPay; the simulator must agree with them."""
    for item in recordings():
        request, expected = item["request"], item["response"]
        headers = AUTH if "Authorization" in request.get("headers", {}) else {}
        response = simulator.request(request["method"], request["path"], headers=headers,
                                     params=request.get("query"), json=request.get("body"))
        assert response.status_code == expected["status"], item["name"]
        assert response.json() == expected["body"], item["name"]


def test_the_simulator_imports_no_api_revenant_code() -> None:
    tree = ast.parse(SIMULATOR.read_text("utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "revenant" not in imported and "tests" not in imported
    assert imported <= {"__future__", "base64", "binascii", "json", "os", "threading", "typing",
                        "fastapi"}


def test_the_simulator_requires_its_own_credential_and_refuses_a_legacy_key(
    simulator: TestClient
) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": NOVAPAY_TOKEN}):
        response = simulator.get("/payments/P-1001", headers=headers)
        assert response.status_code == 401 and response.json()["code"] == "UNAUTHORIZED"
    # A forwarded GravePay key is a visible failure, even beside a valid token.
    forwarded = simulator.get("/payments/P-1001", headers={**AUTH, "X-API-Key": "legacy-demo-key"})
    assert forwarded.status_code == 400 and forwarded.json()["code"] == "UNEXPECTED_CREDENTIAL"


def test_sandboxes_are_isolated_and_reset_to_the_same_seed(simulator: TestClient) -> None:
    private = {**AUTH, "X-NovaPay-Sandbox": "verification"}
    body = {"customer_id": "C1", "transaction": {"value": 5, "currency_code": "ZAR"}}
    first = simulator.post("/payments", headers=private, json=body).json()
    assert first["payment_id"] == "P-2001"
    assert simulator.get("/payments/P-2001", headers=AUTH).status_code == 404
    assert simulator.get("/__sandbox/default/state").json()["payments"] == 5
    assert simulator.get("/__sandbox/verification/state").json()["payments"] == 6

    simulator.post("/payments", headers=AUTH, json=body)
    assert simulator.post("/__sandbox/verification/reset").json()["payments"] == 5
    assert simulator.get("/payments/P-2001", headers=private).status_code == 404
    # The other sandbox is untouched, and the reset one starts from the same ids again.
    assert simulator.get("/payments/P-2001", headers=AUTH).status_code == 200
    again = simulator.post("/payments", headers=private, json=body).json()
    assert again == first


def test_the_receipt_get_has_a_counted_side_effect(simulator: TestClient) -> None:
    def issued() -> int:
        return simulator.get("/__sandbox/default/state").json()["side_effects"][
            "receipts_issued"]

    assert issued() == 0
    assert simulator.get("/payments/P-1001", headers=AUTH).status_code == 200
    assert simulator.get("/payments", headers=AUTH).status_code == 200
    assert issued() == 0  # ordinary reads change nothing
    first = simulator.get("/payments/P-1001/receipt", headers=AUTH).json()
    assert issued() == 1
    assert simulator.get("/payments/P-1001/receipt", headers=AUTH).json() == first
    assert issued() == 1
    events = simulator.get("/payments/P-1001/events", headers=AUTH).json()["items"]
    assert [event["kind"] for event in events] == ["payment.created", "receipt.issued"]


def test_simulator_validation_pagination_and_refund_limits(simulator: TestClient) -> None:
    for body in ({}, {"customer_id": "C1"}, {"customer_id": "C1", "amount": 5},
                 {"customer_id": "C1", "transaction": {"value": 0, "currency_code": "ZAR"}},
                 {"customer_id": "C1", "transaction": {"value": 5, "currency_code": "ZAR"},
                  "customer": "C1"}):
        assert simulator.post("/payments", headers=AUTH, json=body).status_code == 422
    last = simulator.get("/payments", headers=AUTH,
                         params={"cursor": "eyJwYWdlIjozfQ==", "page_size": 2}).json()
    assert [item["payment_id"] for item in last["items"]] == ["P-1005"]
    assert "next_cursor" not in last
    assert simulator.get("/payments", headers=AUTH, params={"cursor": "junk"}).status_code == 422
    assert simulator.get("/payments", headers=AUTH, params={"page_size": 0}).status_code == 422
    refund = {"value": 1000}
    assert simulator.post("/payments/P-1005/refunds", headers=AUTH, json=refund).status_code == 201
    again = simulator.post("/payments/P-1005/refunds", headers=AUTH, json=refund)
    assert again.status_code == 409 and again.json()["code"] == "REFUND_NOT_ALLOWED"
    declined = simulator.post("/payments/P-1004/refunds", headers=AUTH, json={"value": 1})
    assert declined.status_code == 409


def test_the_legacy_client_matches_its_recorded_checksum() -> None:
    recorded = (CLIENT.parent / "SHA256").read_text("ascii").split()[0]
    # Line endings are normalised so the check means the same on every platform.
    content = CLIENT.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(content).hexdigest() == recorded
    source = content.decode("utf-8").lower()
    assert "novapay" in source.split('"""')[1]  # only to say it knows nothing of it
    assert "novapay" not in source.split('"""', 2)[2] and "revenant" not in source.split(
        '"""', 2)[2]


def test_the_legacy_client_fails_against_novapay_directly() -> None:
    """Before: the unchanged client cannot use the replacement API."""
    with serve(load_simulator().app) as url:
        result = run_client(url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED: create charge: expected 201" in result.stdout
    assert "OK:" not in result.stdout


def test_the_legacy_client_reports_an_unreachable_service() -> None:
    result = run_client("http://127.0.0.1:9")
    assert result.returncode == 1 and "cannot reach GravePay" in result.stdout
