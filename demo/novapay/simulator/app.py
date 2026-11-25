"""NovaPay simulator.

NovaPay is a FICTIONAL payment API created for the API Revenant demonstration.
This program is written from demo/novapay/openapi.yaml and imports nothing from
API Revenant, so that it can stand in for an independent replacement service.
It holds deterministic sample data in memory. No real payments are involved.

Run:  uvicorn app:app --port 8100     (from this directory)
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TOKEN = os.environ.get("NOVAPAY_TOKEN", "replacement-demo-token")
CURRENCIES = ("ZAR", "USD", "EUR", "GBP")
PROBLEMS = "https://docs.novapay.demo/problems/"
FIRST_CREATED_AT = 1741944067  # the simulator's clock is a counter, so runs are repeatable
CREATED_STEP = 93

SEED = [
    ("P-1001", "C101", 250, "ZAR", "COMPLETED", 1740816000),
    ("P-1002", "C102", 500, "ZAR", "COMPLETED", 1740907800),
    ("P-1003", "C103", 1200, "USD", "PROCESSING", 1740996900),
    ("P-1004", "C104", 75, "EUR", "DECLINED", 1741088700),
    ("P-1005", "C105", 1500, "ZAR", "COMPLETED", 1741180000),
]


class Sandbox:
    """One isolated data set, always created from the same seed."""

    def __init__(self) -> None:
        self.payments: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, list[dict[str, Any]]] = {}
        self.receipts: dict[str, int] = {}
        self.created = 0
        self.refunded = 0
        self.requests = 0
        for payment_id, customer, value, currency, state, created_at in SEED:
            self.payments[payment_id] = {
                "payment_id": payment_id, "customer_id": customer,
                "transaction": {"value": value, "currency_code": currency},
                "state": state, "created_at": created_at}

    def state(self) -> dict[str, Any]:
        return {"payments": len(self.payments), "requests": self.requests,
                "side_effects": {"payments_created": self.created,
                                 "refunds_created": self.refunded,
                                 "receipts_issued": len(self.receipts)}}


_lock = threading.Lock()
_sandboxes: dict[str, Sandbox] = {}


def sandbox(name: str) -> Sandbox:
    with _lock:
        if name not in _sandboxes:
            _sandboxes[name] = Sandbox()
        return _sandboxes[name]


def problem(status: int, slug: str, title: str, detail: str, code: str) -> JSONResponse:
    return JSONResponse({"type": PROBLEMS + slug, "title": title, "status": status,
                         "detail": detail, "code": code}, status_code=status)


def not_found(payment_id: str) -> JSONResponse:
    return problem(404, "payment-not-found", "Payment not found",
                   f"No such resource: {payment_id}.", "PAYMENT_NOT_FOUND")


def invalid(detail: str) -> JSONResponse:
    return problem(422, "validation-error", "Validation error", detail, "VALIDATION_ERROR")


app = FastAPI(title="NovaPay simulator (fictional)", docs_url=None, redoc_url=None,
              openapi_url=None)


@app.middleware("http")
async def gate(request: Request, call_next: Any) -> Any:
    if request.url.path.startswith("/__sandbox/"):
        return await call_next(request)
    # NovaPay knows nothing of GravePay keys. Receiving one means a legacy
    # credential was forwarded, which the demonstration treats as a failure.
    if "x-api-key" in request.headers:
        return problem(400, "unexpected-credential", "Unexpected credential",
                       "This API does not accept an X-API-Key header.", "UNEXPECTED_CREDENTIAL")
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or value != TOKEN:
        return problem(401, "unauthorized", "Unauthorized", "Missing or invalid credentials.",
                       "UNAUTHORIZED")
    box = sandbox(request.headers.get("x-novapay-sandbox", "default"))
    with _lock:
        box.requests += 1
    request.state.sandbox = box
    return await call_next(request)


@app.post("/__sandbox/{name}/reset")
def reset(name: str) -> dict[str, Any]:
    with _lock:
        _sandboxes[name] = Sandbox()
        return {"sandbox": name, "reset": True, **_sandboxes[name].state()}


@app.get("/__sandbox/{name}/state")
def state(name: str) -> dict[str, Any]:
    return {"sandbox": name, **sandbox(name).state()}


@app.post("/payments", status_code=201)
async def create_payment(request: Request) -> Any:
    box: Sandbox = request.state.sandbox
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return invalid("The request body is not valid JSON.")
    if not isinstance(body, dict):
        return invalid("The request body must be an object.")
    extra = sorted(set(body) - {"customer_id", "transaction", "external_ref"})
    if extra:
        return invalid(f"Unknown field: {extra[0]}.")
    transaction = body.get("transaction")
    if not isinstance(body.get("customer_id"), str) or not isinstance(transaction, dict):
        return invalid("customer_id and transaction are required.")
    value, currency = transaction.get("value"), transaction.get("currency_code")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return invalid("transaction.value must be a positive integer.")
    if not isinstance(currency, str) or len(currency) != 3:
        return invalid("transaction.currency_code must be a three-letter code.")
    if currency not in CURRENCIES:
        return problem(422, "currency-not-supported", "Currency not supported",
                       f"Currency {currency} is not supported.", "CURRENCY_NOT_SUPPORTED")
    reference = body.get("external_ref")
    if reference is not None and not isinstance(reference, str):
        return invalid("external_ref must be a string.")
    with _lock:
        box.created += 1
        payment_id = f"P-{2000 + box.created}"
        payment = {"payment_id": payment_id, "customer_id": body["customer_id"],
                   "transaction": {"value": value, "currency_code": currency},
                   "state": "COMPLETED",
                   "created_at": FIRST_CREATED_AT + CREATED_STEP * (box.created - 1)}
        if reference is not None:
            payment["external_ref"] = reference
        box.payments[payment_id] = payment
    return payment


@app.get("/payments")
def list_payments(request: Request, cursor: str | None = None, page_size: int = 20) -> Any:
    box: Sandbox = request.state.sandbox
    if not 1 <= page_size <= 100:
        return invalid("page_size must be between 1 and 100.")
    page = 1
    if cursor is not None:
        try:
            decoded = json.loads(base64.b64decode(cursor, validate=True))
            page = decoded["page"]
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise ValueError
        except (binascii.Error, ValueError, KeyError, TypeError):
            return problem(422, "invalid-cursor", "Invalid cursor",
                           "The cursor is not valid.", "INVALID_CURSOR")
    ordered = sorted(box.payments.values(), key=lambda item: item["created_at"])
    start = (page - 1) * page_size
    body: dict[str, Any] = {"items": ordered[start:start + page_size]}
    if start + page_size < len(ordered):
        body["next_cursor"] = base64.b64encode(
            json.dumps({"page": page + 1}, separators=(",", ":")).encode()).decode()
    return body


@app.get("/payments/{payment_id}")
def get_payment(payment_id: str, request: Request) -> Any:
    payment = request.state.sandbox.payments.get(payment_id)
    return payment if payment is not None else not_found(payment_id)


@app.post("/payments/{payment_id}/refunds", status_code=201)
async def create_refund(payment_id: str, request: Request) -> Any:
    box: Sandbox = request.state.sandbox
    payment = box.payments.get(payment_id)
    if payment is None:
        return not_found(payment_id)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return invalid("The request body is not valid JSON.")
    value = body.get("value") if isinstance(body, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return invalid("value must be a positive integer.")
    with _lock:
        already = sum(item["value"] for item in box.refunds.get(payment_id, []))
        if payment["state"] != "COMPLETED" or already + value > payment["transaction"]["value"]:
            return problem(409, "refund-not-allowed", "Refund not allowed",
                           "The payment cannot be refunded by this amount.",
                           "REFUND_NOT_ALLOWED")
        box.refunded += 1
        refund = {"refund_id": f"R-{3000 + box.refunded}", "payment_id": payment_id,
                  "value": value, "state": "COMPLETED"}
        box.refunds.setdefault(payment_id, []).append(refund)
    return refund


@app.get("/payments/{payment_id}/receipt")
def get_receipt(payment_id: str, request: Request) -> Any:
    """A GET with a side effect: the first call issues the receipt and records when."""
    box: Sandbox = request.state.sandbox
    payment = box.payments.get(payment_id)
    if payment is None:
        return not_found(payment_id)
    with _lock:
        if payment_id not in box.receipts:
            box.receipts[payment_id] = payment["created_at"] + 60
    return {"payment_id": payment_id, "receipt_no": "RCPT-" + payment_id.split("-")[-1],
            "receipt_issued_at": box.receipts[payment_id]}


@app.get("/payments/{payment_id}/events")
def list_events(payment_id: str, request: Request) -> Any:
    box: Sandbox = request.state.sandbox
    payment = box.payments.get(payment_id)
    if payment is None:
        return not_found(payment_id)
    events = [{"kind": "payment.created", "occurred_at": payment["created_at"]}]
    if payment_id in box.receipts:
        events.append({"kind": "receipt.issued", "occurred_at": box.receipts[payment_id]})
    events += [{"kind": "refund.created", "occurred_at": payment["created_at"] + 120 + index}
               for index, _ in enumerate(box.refunds.get(payment_id, []))]
    return {"items": events}
