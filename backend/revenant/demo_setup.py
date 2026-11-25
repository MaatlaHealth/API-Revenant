"""Set up the demonstration through the public management API.

GravePay and NovaPay are fictional. This program holds no privileged path into
the product: it does what a person would do in the interface, in the same
order, using the admin token. Used by ``demo/run_demo.py`` and by the Docker
demonstration.

    python -m revenant.demo_setup --api http://revenant:8000
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from typing import Any

import httpx

# The inferred mappings a reviewer approves by hand in this demonstration. The
# events operation has no recorded exchange and is deliberately left out.
REVIEWED = (
    "pagination:GET /v1/charges",
    "field:POST /v1/charges:response.body:$.status",
    "field:GET /v1/charges/{id}:response.body:$.status",
    "field:POST /v1/charges/{id}/refund:response.body:$.status",
)
SECRET_REF = "replacement_credential"  # noqa: S105 - the name of a credential, not its value


def set_up(api: httpx.Client, *, upstream: str, legacy_key: str, replacement_token: str,
           legacy_hosts: list[str], say: Callable[[str], None] = print) -> dict[str, Any]:
    """Seed, reconstruct, review, compile, store credentials and start the runtime.

    Safe to run again: each step is skipped or repeated harmlessly. Returns the
    runtime description.
    """
    def call(method: str, path: str, **kwargs: Any) -> Any:
        response = api.request(method, path, **kwargs)
        if response.status_code >= 400:
            # Status and error code only: a response body is not echoed.
            code = ""
            try:
                code = response.json().get("error", {}).get("code", "")
            except ValueError:
                pass
            raise SystemExit(f"{method} {path} failed: {response.status_code} {code}")
        return response.json() if response.content else {}

    project = call("POST", "/api/v1/demo/seed")
    root = f"/api/v1/projects/{project['id']}"
    say(f"project {project['slug']}: {project['evidence']['files']} evidence files")
    call("PATCH", root, json={"replacement_base_url": upstream})
    summary = call("POST", f"{root}/reconstructions")["summary"]
    say(f"reconstruction: {summary['mappings']['total']} mappings "
        f"({summary['mappings']['observed']} observed, {summary['mappings']['inferred']} "
        f"inferred), {summary['gaps']['total']} gaps, {summary['behaviours']['total']} behaviours")
    approved = call("POST", f"{root}/mappings/bulk-approve",
                    json={"filter": {"confidence": "observed"}})["approved_count"]
    mappings = {item["key"]: item["id"]
                for item in call("GET", f"{root}/mappings", params={"limit": 200})["items"]}
    for key in REVIEWED:
        call("POST", f"{root}/mappings/{mappings[key]}/approve",
             json={"note": "Reviewed for the demonstration."})
    say(f"approved {approved} observed mappings and {len(REVIEWED)} inferred ones")
    adapter = call("POST", f"{root}/adapters")
    say(f"adapter version {adapter['version']}: {adapter['routes']} routes")
    call("PUT", f"{root}/credentials/outbound/{SECRET_REF}", json={"value": replacement_token})
    labels = {item["label"] for item in call("GET", f"{root}/credentials/inbound")["items"]
              if item["status"] == "active"}
    if "legacy client" not in labels:
        call("POST", f"{root}/credentials/inbound",
             json={"label": "legacy client", "value": legacy_key})
    if legacy_hosts:
        call("PATCH", f"{root}/runtime", json={"legacy_hosts": legacy_hosts})
    runtime: dict[str, Any] = call("POST", f"{root}/runtime/start")
    say(f"runtime {runtime['status']}: path entry {runtime['path_entry']}, "
        f"legacy hosts {', '.join(runtime['legacy_hosts']) or 'none'}")
    return {**runtime, "project_id": project["id"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, help="base URL of the API Revenant backend")
    parser.add_argument("--upstream", default="http://api.novapay.demo:8000")
    parser.add_argument("--legacy-host", action="append", default=None)
    args = parser.parse_args(argv)
    token = os.environ.get("REVENANT_ADMIN_TOKEN", "")
    if not token:
        print("REVENANT_ADMIN_TOKEN is not set")
        return 2
    hosts = args.legacy_host if args.legacy_host is not None else ["api.gravepay.demo"]
    with httpx.Client(base_url=args.api, timeout=60, trust_env=False,
                      headers={"Authorization": f"Bearer {token}"}) as api:
        set_up(api, upstream=args.upstream,
               legacy_key=os.environ.get("DEMO_LEGACY_API_KEY", "legacy-demo-key"),
               replacement_token=os.environ.get("DEMO_REPLACEMENT_TOKEN",
                                                "replacement-demo-token"),
               legacy_hosts=hosts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
