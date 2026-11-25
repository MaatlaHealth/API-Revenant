"""GravePay client.

Written once against the GravePay API (a FICTIONAL, discontinued payment API
created for the API Revenant demonstration) and not edited since: the file
SHA256 beside it holds its checksum, and a test compares the two.

It knows nothing about NovaPay or API Revenant. It creates a charge, reads it
back, lists charges, handles an invalid-currency error by its GravePay error
code, and issues a refund. Any response that is not what GravePay used to send
ends the program with a non-zero status.
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("GRAVEPAY_BASE_URL", "http://api.gravepay.demo:8000").rstrip("/")
API_KEY = os.environ.get("GRAVEPAY_API_KEY", "legacy-demo-key")
STATUSES = ("succeeded", "failed", "pending")


class Unexpected(Exception):
    pass


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(BASE_URL + path, data=data, method=method)
    request.add_header("X-API-Key", API_KEY)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        raise Unexpected("%s %s: cannot reach GravePay (%s)" % (method, path, error))
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        raise Unexpected("%s %s: status %s with a body that is not JSON" % (method, path, status))


def expect(condition, message):
    if not condition:
        raise Unexpected(message)


def check_charge(charge, where):
    expect(isinstance(charge, dict), "%s: the charge is not an object" % where)
    for field in ("id", "customer", "amount", "currency", "status", "created"):
        expect(field in charge, "%s: the charge has no %r" % (where, field))
    expect(charge["status"] in STATUSES, "%s: unknown status %r" % (where, charge["status"]))
    expect(isinstance(charge["amount"], int), "%s: amount is not an integer" % where)
    created = charge["created"]
    expect(isinstance(created, str) and len(created) == 20 and created[10] == "T"
           and created.endswith("Z"), "%s: created is not a timestamp: %r" % (where, created))


def main():
    print("GravePay client -> %s" % BASE_URL)

    status, charge = call("POST", "/v1/charges", {
        "customer": "C210", "amount": 500, "currency": "ZAR", "reference": "order-7781"})
    expect(status == 201, "create charge: expected 201, got %s %s" % (status, charge))
    check_charge(charge, "create charge")
    expect(charge["customer"] == "C210" and charge["amount"] == 500
           and charge["currency"] == "ZAR" and charge.get("reference") == "order-7781",
           "create charge: the charge does not match the request: %s" % charge)
    charge_id = charge["id"]
    print("1. created charge %s (%s)" % (charge_id, charge["status"]))

    status, fetched = call("GET", "/v1/charges/%s" % charge_id)
    expect(status == 200, "retrieve charge: expected 200, got %s %s" % (status, fetched))
    check_charge(fetched, "retrieve charge")
    expect(fetched["id"] == charge_id and fetched["amount"] == 500,
           "retrieve charge: not the charge that was created: %s" % fetched)
    print("2. retrieved charge %s, created %s" % (fetched["id"], fetched["created"]))

    status, page = call("GET", "/v1/charges?page=2&limit=2")
    expect(status == 200, "list charges: expected 200, got %s %s" % (status, page))
    expect(isinstance(page, dict) and isinstance(page.get("data"), list),
           "list charges: no data list in %s" % page)
    expect(page.get("page") == 2 and page.get("limit") == 2
           and isinstance(page.get("has_more"), bool),
           "list charges: page, limit or has_more is wrong in %s" % page)
    expect(len(page["data"]) == 2, "list charges: expected 2 charges on page 2")
    for item in page["data"]:
        check_charge(item, "list charges")
    print("3. listed page 2: %s" % ", ".join(item["id"] for item in page["data"]))

    status, error = call("POST", "/v1/charges", {
        "customer": "C210", "amount": 500, "currency": "XXX"})
    expect(status == 400, "invalid currency: expected 400, got %s %s" % (status, error))
    code = (error or {}).get("error", {}).get("code") if isinstance(error, dict) else None
    expect(code == "invalid_currency",
           "invalid currency: expected error code invalid_currency, got %s" % error)
    print("4. invalid currency rejected with code %s" % code)

    status, refund = call("POST", "/v1/charges/%s/refund" % charge_id, {"amount": 200})
    expect(status == 201, "refund: expected 201, got %s %s" % (status, refund))
    expect(isinstance(refund, dict) and refund.get("charge") == charge_id
           and refund.get("amount") == 200 and refund.get("status") in STATUSES
           and "id" in refund, "refund: unexpected refund %s" % refund)
    print("5. refunded 200 of charge %s (refund %s)" % (charge_id, refund["id"]))

    print("OK: GravePay behaved as this client expects.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Unexpected as failure:
        print("FAILED: %s" % failure)
        sys.exit(1)
