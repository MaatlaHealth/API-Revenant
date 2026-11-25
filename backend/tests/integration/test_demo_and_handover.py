"""The demonstration seed and runner, and a lease handover between two running
instances (requirements 7.28, 7.29, 7.36, 9.1 to 9.3)."""

from __future__ import annotations

import os
import subprocess
import sys

import httpx
from fastapi.testclient import TestClient

from revenant.main import create_app
from tests.conftest import make_settings
from tests.integration.test_evidence_api import base
from tests.integration.test_runtime_e2e import ADMIN, KEY, prepare, runtime_settings
from tests.live import REPO, load_simulator, serve


def test_seeding_the_demonstration_is_idempotent_and_labelled(
    client: TestClient, admin: dict[str, str]
) -> None:
    assert client.post("/api/v1/demo/seed").status_code == 401
    first = client.post("/api/v1/demo/seed", headers=admin)
    assert first.status_code == 200, first.text
    project = first.json()
    assert project["slug"] == "gravepay-demo" and project["is_demo"] is True
    assert "fictional" in project["legacy_name"] and "fictional" in project["replacement_name"]
    assert project["evidence"] == {"files": 5, "legacy_files": 3, "replacement_files": 2,
                                   "failed_files": 0, "endpoints": 12, "fixtures": 20}
    again = client.post("/api/v1/demo/seed", headers=admin).json()
    assert again["id"] == project["id"] and again["evidence"] == project["evidence"]
    assert again["evidence_version"] == project["evidence_version"]
    listed = client.get("/api/v1/projects", headers=admin).json()
    assert listed["total"] == 1
    actions = [item["action"] for item in client.get(
        f"{base(project['id'])}/activity", headers=admin).json()["items"]]
    assert actions.count("demo.seed") == 1
    # The seeded evidence reconstructs like any other.
    rebuilt = client.post(f"{base(project['id'])}/reconstructions", headers=admin)
    assert rebuilt.status_code == 201 and rebuilt.json()["summary"]["mappings"]["total"] == 55


def test_the_seed_does_not_take_over_an_ordinary_project(
    client: TestClient, admin: dict[str, str]
) -> None:
    created = client.post("/api/v1/projects", headers=admin, json={
        "name": "Mine", "slug": "gravepay-demo", "legacy_name": "A", "replacement_name": "B"})
    assert created.status_code == 201
    refused = client.post("/api/v1/demo/seed", headers=admin)
    assert refused.status_code == 409
    assert client.get(f"{base(created.json()['id'])}/evidence",
                      headers=admin).json()["total"] == 0


def test_the_seed_reports_missing_demonstration_files(migrated_url: str, tmp_path: object,
                                                      admin: dict[str, str]) -> None:
    app = create_app(make_settings(migrated_url, demo_dir=str(tmp_path)), supervised=False)
    with TestClient(app) as client:
        response = client.post("/api/v1/demo/seed", headers=admin)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_configured"
        assert client.get("/api/v1/projects", headers=admin).json()["total"] == 0


def test_the_demonstration_runner_shows_before_and_after() -> None:
    """``demo/run_demo.py`` starts both services as real processes and drives the
    whole workflow through the management API."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, str(REPO / "demo" / "run_demo.py")],  # noqa: S603
                            capture_output=True, text=True, timeout=300, env=env,
                            encoding="utf-8")
    output = result.stdout
    assert result.returncode == 0, output + result.stderr
    assert "(match)" in output
    before, after = output.split("AFTER")[0], output.split("AFTER")[1]
    assert "FAILED: create charge: expected 201" in before
    assert "exit status 1  ->  failed, as expected" in before
    assert "reconstruction: 55 mappings (47 observed, 8 inferred), 11 gaps" in before
    assert "OK: GravePay behaved as this client expects." in after
    assert "exit status 0  ->  succeeded" in after
    assert "5 requests recorded by the runtime" in after
    assert "RESULT  the unchanged client failed before and succeeded after." in after
    # Neither demonstration credential is printed.
    assert "legacy-demo-key" not in output and "replacement-demo-token" not in output


def test_a_starting_instance_takes_over_only_after_the_holder_has_drained(
    migrated_url: str, project_id: str
) -> None:
    """Two instances on one database, as during a deployment. The second asks for a
    handover and waits; the first stops accepting, drains and releases; only then
    does the second serve. At no moment do both accept runtime traffic."""
    simulator = load_simulator()
    with serve(simulator.app) as upstream:
        settings = runtime_settings(migrated_url, upstream, lease_renew_seconds=0.2)
        old = create_app(settings, supervised=False)
        with serve(old.state.dispatcher) as first, httpx.Client(
                base_url=first, headers=ADMIN, timeout=60) as admin:
            prepare(admin, project_id, upstream)
            url = "/runtime/gravepay/v1/charges/P-1001"
            assert httpx.get(first + url, headers=KEY).status_code == 200
            assert old.state.lease.epoch == 1

            new = create_app(settings, supervised=False)
            new.state.lease.host = "another-machine"     # not a sibling worker
            # serve() returns once the new instance has finished starting, which it
            # can only do after acquiring the lease.
            with serve(new.state.dispatcher) as second:
                assert new.state.lease.state == "held" and new.state.lease.epoch == 2
                assert old.state.lease.state == "released"
                # The old instance serves nothing and is not ready; it does not take
                # the lease back.
                refused = httpx.get(first + url, headers=KEY)
                assert refused.status_code == 503
                assert refused.headers["x-revenant-runtime"] == "superseded"
                assert httpx.get(first + "/readyz").status_code == 503
                # The new instance recovered the running runtime by itself.
                served = httpx.get(second + url, headers=KEY)
                assert served.status_code == 200 and served.json()["id"] == "P-1001"
                assert httpx.get(second + "/readyz").status_code == 200
                assert old.state.lease.tick() is False
                assert new.state.lease.renew() is True
